#!/usr/bin/env node

import { type ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { chmod, mkdir, readFile, rename, stat, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join, resolve } from "node:path";
import type { Readable } from "node:stream";

interface PiConfig {
	command: string;
	cwd: string;
	sessionDir: string;
	sessionId: string;
	sessionName: string;
	approveProject: boolean;
	responseTimeoutMs: number;
	systemPrompt: string;
	args: string[];
}

interface GatewayConfig {
	appId: string;
	allowFrom: Set<string>;
	adminFrom: Set<string>;
	directChatId: string;
	stateDir: string;
	larkCliCommand: string;
	pi: PiConfig;
}

interface LarkMessageEvent {
	type: "im.message.receive_v1";
	chatId: string;
	chatType: "p2p";
	content: string;
	messageId: string;
	senderId: string;
	senderType: "user";
}

interface PendingRequest {
	resolve: (data: unknown) => void;
	reject: (error: Error) => void;
	timer: NodeJS.Timeout;
}

interface CliOptions {
	configPath: string;
	once: boolean;
	validateOnly: boolean;
}

const DEFAULT_SYSTEM_PROMPT =
	"You are Pi in a private Feishu direct chat. Keep replies concise and suitable for chat. Only use tools when needed. Before destructive actions or external communications, require explicit confirmation unless the current user message already explicitly requests that exact action.";
const MAX_JSONL_RECORD_BYTES = 16 * 1024 * 1024;
const MAX_PROCESSED_MESSAGE_IDS = 1000;
const MAX_FEISHU_TEXT_CHARS = 20_000;

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function getRequiredString(value: Record<string, unknown>, key: string): string {
	const result = value[key];
	if (typeof result !== "string" || result.length === 0) {
		throw new Error(`Config field ${key} must be a non-empty string`);
	}
	return result;
}

function getOptionalString(value: Record<string, unknown>, key: string, fallback: string): string {
	const result = value[key];
	if (result === undefined) return fallback;
	if (typeof result !== "string" || result.length === 0) {
		throw new Error(`Config field ${key} must be a non-empty string`);
	}
	return result;
}

function getStringArray(value: Record<string, unknown>, key: string): string[] {
	const result = value[key];
	if (!Array.isArray(result) || result.length === 0 || !result.every((entry) => typeof entry === "string" && entry)) {
		throw new Error(`Config field ${key} must be a non-empty string array`);
	}
	return [...new Set(result)];
}

function expandHome(value: string): string {
	if (value === "~") return homedir();
	if (value.startsWith("~/")) return join(homedir(), value.slice(2));
	return value;
}

async function loadConfig(configPath: string): Promise<GatewayConfig> {
	const absolutePath = resolve(expandHome(configPath));
	const fileStat = await stat(absolutePath);
	if ((fileStat.mode & 0o077) !== 0) {
		throw new Error(`Config must not be accessible by group or others: ${absolutePath}`);
	}

	const parsed: unknown = JSON.parse(await readFile(absolutePath, "utf8"));
	if (!isRecord(parsed)) throw new Error("Config root must be an object");

	const appId = getRequiredString(parsed, "app_id");
	const allowFrom = new Set(getStringArray(parsed, "allow_from"));
	const adminFrom = new Set(getStringArray(parsed, "admin_from"));
	const directChatId = getRequiredString(parsed, "direct_chat_id");
	if (!/^cli_[A-Za-z0-9]+$/.test(appId)) throw new Error("app_id must start with cli_");
	if (![...allowFrom].every((id) => /^ou_[A-Za-z0-9]+$/.test(id))) {
		throw new Error("Every allow_from entry must start with ou_");
	}
	if (![...adminFrom].every((id) => allowFrom.has(id))) {
		throw new Error("Every admin_from entry must also appear in allow_from");
	}
	if (!/^oc_[A-Za-z0-9]+$/.test(directChatId)) throw new Error("direct_chat_id must start with oc_");

	const rawPi = parsed.pi;
	if (!isRecord(rawPi)) throw new Error("Config field pi must be an object");
	const rawArgs = rawPi.args;
	if (rawArgs !== undefined && (!Array.isArray(rawArgs) || !rawArgs.every((entry) => typeof entry === "string"))) {
		throw new Error("Config field pi.args must be a string array");
	}
	const rawTimeout = rawPi.response_timeout_ms;
	if (
		rawTimeout !== undefined &&
		(typeof rawTimeout !== "number" || !Number.isInteger(rawTimeout) || rawTimeout < 1000 || rawTimeout > 1_800_000)
	) {
		throw new Error("Config field pi.response_timeout_ms must be an integer from 1000 to 1800000");
	}
	const rawApproveProject = rawPi.approve_project;
	if (rawApproveProject !== undefined && typeof rawApproveProject !== "boolean") {
		throw new Error("Config field pi.approve_project must be a boolean");
	}

	return {
		appId,
		allowFrom,
		adminFrom,
		directChatId,
		stateDir: resolve(expandHome(getOptionalString(parsed, "state_dir", "~/.pi/agent/feishu-gateway"))),
		larkCliCommand: expandHome(getOptionalString(parsed, "lark_cli_command", "lark-cli")),
		pi: {
			command: expandHome(getOptionalString(rawPi, "command", "pi")),
			cwd: resolve(expandHome(getRequiredString(rawPi, "cwd"))),
			sessionDir: resolve(
				expandHome(getOptionalString(rawPi, "session_dir", "~/.pi/agent/feishu-gateway/sessions")),
			),
			sessionId: getOptionalString(rawPi, "session_id", "feishu-gateway"),
			sessionName: getOptionalString(rawPi, "session_name", "Feishu gateway"),
			approveProject: rawApproveProject ?? false,
			responseTimeoutMs: rawTimeout ?? 600_000,
			systemPrompt: getOptionalString(rawPi, "system_prompt", DEFAULT_SYSTEM_PROMPT),
			args: rawArgs === undefined ? [] : [...rawArgs],
		},
	};
}

function attachJsonlReader(
	stream: Readable,
	onLine: (line: string) => void,
	onError: (error: Error) => void,
): () => void {
	let buffer = Buffer.alloc(0);

	const handleData = (chunk: Buffer | string): void => {
		const next = typeof chunk === "string" ? Buffer.from(chunk) : chunk;
		buffer = Buffer.concat([buffer, next]);
		if (buffer.length > MAX_JSONL_RECORD_BYTES && buffer.indexOf(0x0a) === -1) {
			onError(new Error(`JSONL record exceeded ${MAX_JSONL_RECORD_BYTES} bytes`));
			return;
		}

		let newlineIndex = buffer.indexOf(0x0a);
		while (newlineIndex !== -1) {
			let line = buffer.subarray(0, newlineIndex);
			buffer = buffer.subarray(newlineIndex + 1);
			if (line.at(-1) === 0x0d) line = line.subarray(0, -1);
			if (line.length > 0) onLine(line.toString("utf8"));
			newlineIndex = buffer.indexOf(0x0a);
		}
	};
	const handleEnd = (): void => {
		if (buffer.length > 0) {
			const line = buffer.at(-1) === 0x0d ? buffer.subarray(0, -1) : buffer;
			if (line.length > 0) onLine(line.toString("utf8"));
		}
		buffer = Buffer.alloc(0);
	};

	stream.on("data", handleData);
	stream.on("end", handleEnd);
	return () => {
		stream.off("data", handleData);
		stream.off("end", handleEnd);
	};
}

class ProcessedMessageStore {
	private readonly path: string;
	private ids: string[] = [];
	private readonly idSet = new Set<string>();

	constructor(stateDir: string) {
		this.path = join(stateDir, "processed-message-ids.json");
	}

	async load(): Promise<void> {
		try {
			const parsed: unknown = JSON.parse(await readFile(this.path, "utf8"));
			if (!Array.isArray(parsed) || !parsed.every((value) => typeof value === "string")) {
				throw new Error("processed message state must be a string array");
			}
			this.ids = parsed.slice(-MAX_PROCESSED_MESSAGE_IDS);
			for (const id of this.ids) this.idSet.add(id);
		} catch (error: unknown) {
			if (isRecord(error) && error.code === "ENOENT") return;
			throw error;
		}
	}

	has(id: string): boolean {
		return this.idSet.has(id);
	}

	async add(id: string): Promise<void> {
		if (this.idSet.has(id)) return;
		this.ids.push(id);
		this.idSet.add(id);
		while (this.ids.length > MAX_PROCESSED_MESSAGE_IDS) {
			const removed = this.ids.shift();
			if (removed) this.idSet.delete(removed);
		}
		const temporaryPath = `${this.path}.tmp-${process.pid}`;
		await writeFile(temporaryPath, `${JSON.stringify(this.ids, null, 2)}\n`, { mode: 0o600 });
		await rename(temporaryPath, this.path);
	}
}

class PiRpcClient {
	private readonly config: PiConfig;
	private readonly onFatal: (error: Error) => void;
	private child: ChildProcessWithoutNullStreams | null = null;
	private detachReader: (() => void) | null = null;
	private requestCounter = 0;
	private readonly pending = new Map<string, PendingRequest>();
	private settledWaiters: Array<{ resolve: () => void; reject: (error: Error) => void; timer: NodeJS.Timeout }> = [];
	private stopping = false;
	private stderr = "";

	constructor(config: PiConfig, onFatal: (error: Error) => void) {
		this.config = config;
		this.onFatal = onFatal;
	}

	async start(): Promise<void> {
		await mkdir(this.config.sessionDir, { recursive: true, mode: 0o700 });
		await chmod(this.config.sessionDir, 0o700);
		const args = [
			"--mode",
			"rpc",
			"--session-id",
			this.config.sessionId,
			"--session-dir",
			this.config.sessionDir,
			"--name",
			this.config.sessionName,
			"--append-system-prompt",
			this.config.systemPrompt,
			this.config.approveProject ? "--approve" : "--no-approve",
			...this.config.args,
		];
		const child = spawn(this.config.command, args, {
			cwd: this.config.cwd,
			env: process.env,
			stdio: ["pipe", "pipe", "pipe"],
		});
		this.child = child;
		this.detachReader = attachJsonlReader(
			child.stdout,
			(line) => this.handleLine(line),
			(error) => this.fail(error),
		);
		child.stderr.on("data", (chunk: Buffer | string) => {
			const text = chunk.toString();
			this.stderr = `${this.stderr}${text}`.slice(-65_536);
			process.stderr.write(`[pi] ${text}`);
		});
		child.once("error", (error) => this.fail(new Error(`Failed to start Pi: ${error.message}`)));
		child.once("exit", (code, signal) => {
			if (!this.stopping) this.fail(new Error(`Pi exited (code=${code}, signal=${signal})`));
		});
		await this.request({ type: "get_state" }, 60_000);
	}

	async promptAndGetText(message: string): Promise<string> {
		const settled = this.waitForSettled(this.config.responseTimeoutMs);
		try {
			await this.request({ type: "prompt", message });
			await settled;
		} catch (error) {
			await this.request({ type: "abort" }).catch(() => undefined);
			throw error;
		}
		const data = await this.request({ type: "get_last_assistant_text" });
		if (!isRecord(data) || (data.text !== null && typeof data.text !== "string")) {
			throw new Error("Pi returned an invalid get_last_assistant_text response");
		}
		return data.text?.trim() || "Pi 没有返回文本结果。";
	}

	async newSession(): Promise<void> {
		await this.request({ type: "new_session" });
	}

	async status(): Promise<string> {
		const data = await this.request({ type: "get_state" });
		if (!isRecord(data)) throw new Error("Pi returned an invalid state response");
		const model = isRecord(data.model) ? data.model : undefined;
		const provider = typeof model?.provider === "string" ? model.provider : "unknown";
		const modelId = typeof model?.id === "string" ? model.id : "unknown";
		const sessionName = typeof data.sessionName === "string" ? data.sessionName : this.config.sessionName;
		const messageCount = typeof data.messageCount === "number" ? data.messageCount : 0;
		return `Pi 状态：${provider}/${modelId}\n会话：${sessionName}\n消息数：${messageCount}`;
	}

	async stop(): Promise<void> {
		this.stopping = true;
		this.detachReader?.();
		this.detachReader = null;
		const child = this.child;
		this.child = null;
		if (!child || child.exitCode !== null) return;
		child.kill("SIGTERM");
		await new Promise<void>((resolveStop) => {
			const timer = setTimeout(() => {
				child.kill("SIGKILL");
				resolveStop();
			}, 2000);
			child.once("exit", () => {
				clearTimeout(timer);
				resolveStop();
			});
		});
	}

	private handleLine(line: string): void {
		let data: unknown;
		try {
			data = JSON.parse(line);
		} catch {
			return;
		}
		if (!isRecord(data)) return;

		if (data.type === "extension_ui_request" && typeof data.id === "string" && typeof data.method === "string") {
			if (["select", "input", "editor"].includes(data.method)) {
				this.write({ type: "extension_ui_response", id: data.id, cancelled: true });
			} else if (data.method === "confirm") {
				this.write({ type: "extension_ui_response", id: data.id, confirmed: false });
			}
			return;
		}

		if (data.type === "response" && typeof data.id === "string") {
			const pending = this.pending.get(data.id);
			if (!pending) return;
			this.pending.delete(data.id);
			clearTimeout(pending.timer);
			if (data.success === true) {
				pending.resolve(data.data);
			} else {
				pending.reject(new Error(typeof data.error === "string" ? data.error : "Pi RPC command failed"));
			}
			return;
		}

		if (data.type === "agent_settled") {
			const waiters = this.settledWaiters;
			this.settledWaiters = [];
			for (const waiter of waiters) {
				clearTimeout(waiter.timer);
				waiter.resolve();
			}
		}
	}

	private request(command: Record<string, unknown>, timeoutMs = 30_000): Promise<unknown> {
		const id = `gateway_${++this.requestCounter}`;
		return new Promise((resolveRequest, rejectRequest) => {
			const timer = setTimeout(() => {
				this.pending.delete(id);
				rejectRequest(new Error(`Timed out waiting for Pi RPC command ${String(command.type)}`));
			}, timeoutMs);
			this.pending.set(id, { resolve: resolveRequest, reject: rejectRequest, timer });
			try {
				this.write({ ...command, id });
			} catch (error: unknown) {
				clearTimeout(timer);
				this.pending.delete(id);
				rejectRequest(error instanceof Error ? error : new Error(String(error)));
			}
		});
	}

	private write(value: Record<string, unknown>): void {
		const child = this.child;
		if (!child || child.stdin.destroyed || !child.stdin.writable) {
			throw new Error(`Pi RPC stdin is unavailable. ${this.stderr}`);
		}
		child.stdin.write(`${JSON.stringify(value)}\n`);
	}

	private waitForSettled(timeoutMs: number): Promise<void> {
		return new Promise((resolveWaiter, rejectWaiter) => {
			const waiter = {
				resolve: resolveWaiter,
				reject: rejectWaiter,
				timer: setTimeout(() => {
					this.settledWaiters = this.settledWaiters.filter((candidate) => candidate !== waiter);
					rejectWaiter(new Error(`Timed out waiting ${timeoutMs}ms for Pi to settle`));
				}, timeoutMs),
			};
			this.settledWaiters.push(waiter);
		});
	}

	private fail(error: Error): void {
		if (this.stopping) return;
		for (const pending of this.pending.values()) {
			clearTimeout(pending.timer);
			pending.reject(error);
		}
		this.pending.clear();
		for (const waiter of this.settledWaiters) {
			clearTimeout(waiter.timer);
			waiter.reject(error);
		}
		this.settledWaiters = [];
		this.onFatal(error);
	}
}

function parseLarkEvent(line: string): LarkMessageEvent | null {
	let parsed: unknown;
	try {
		parsed = JSON.parse(line);
	} catch {
		return null;
	}
	if (!isRecord(parsed)) return null;
	if (
		parsed.type !== "im.message.receive_v1" ||
		parsed.chat_type !== "p2p" ||
		parsed.sender_type !== "user" ||
		typeof parsed.chat_id !== "string" ||
		typeof parsed.sender_id !== "string" ||
		typeof parsed.message_id !== "string" ||
		typeof parsed.content !== "string"
	) {
		return null;
	}
	return {
		type: parsed.type,
		chatId: parsed.chat_id,
		chatType: parsed.chat_type,
		content: parsed.content,
		messageId: parsed.message_id,
		senderId: parsed.sender_id,
		senderType: parsed.sender_type,
	};
}

async function execute(
	command: string,
	args: string[],
	timeoutMs: number,
): Promise<{ stdout: string; stderr: string }> {
	return new Promise((resolveExecution, rejectExecution) => {
		const child = spawn(command, args, { env: process.env, stdio: ["ignore", "pipe", "pipe"] });
		let stdout = "";
		let stderr = "";
		child.stdout.on("data", (chunk: Buffer | string) => {
			stdout = `${stdout}${chunk.toString()}`.slice(-1_000_000);
		});
		child.stderr.on("data", (chunk: Buffer | string) => {
			stderr = `${stderr}${chunk.toString()}`.slice(-1_000_000);
		});
		const timer = setTimeout(() => {
			child.kill("SIGKILL");
			rejectExecution(new Error(`Command timed out: ${command}`));
		}, timeoutMs);
		child.once("error", (error) => {
			clearTimeout(timer);
			rejectExecution(error);
		});
		child.once("exit", (code, signal) => {
			clearTimeout(timer);
			if (code === 0) resolveExecution({ stdout, stderr });
			else rejectExecution(new Error(`${command} exited (code=${code}, signal=${signal}): ${stderr.trim()}`));
		});
	});
}

async function addTypingReaction(config: GatewayConfig, messageId: string): Promise<string | null> {
	try {
		const result = await execute(
			config.larkCliCommand,
			[
				"--profile",
				config.appId,
				"im",
				"reactions",
				"create",
				"--as",
				"bot",
				"--params",
				JSON.stringify({ message_id: messageId }),
				"--data",
				JSON.stringify({ reaction_type: { emoji_type: "Typing" } }),
			],
			60_000,
		);
		const parsed: unknown = JSON.parse(result.stdout);
		const data = isRecord(parsed) && isRecord(parsed.data) ? parsed.data : undefined;
		if (isRecord(parsed) && parsed.ok === true && typeof data?.reaction_id === "string") {
			return data.reaction_id;
		}
		throw new Error("lark-cli did not return a Typing reaction ID");
	} catch (error: unknown) {
		console.error("[gateway] failed to add Typing reaction", error);
		return null;
	}
}

async function removeTypingReaction(config: GatewayConfig, messageId: string, reactionId: string): Promise<void> {
	try {
		await execute(
			config.larkCliCommand,
			[
				"--profile",
				config.appId,
				"im",
				"reactions",
				"delete",
				"--as",
				"bot",
				"--params",
				JSON.stringify({ message_id: messageId, reaction_id: reactionId }),
			],
			60_000,
		);
	} catch (error: unknown) {
		console.error("[gateway] failed to remove Typing reaction", error);
	}
}

async function sendLarkText(config: GatewayConfig, chatId: string, text: string, messageId: string): Promise<void> {
	const characters = [...text];
	const chunks: string[] = [];
	for (let offset = 0; offset < characters.length; offset += MAX_FEISHU_TEXT_CHARS) {
		chunks.push(characters.slice(offset, offset + MAX_FEISHU_TEXT_CHARS).join(""));
	}
	if (chunks.length === 0) chunks.push("Pi 没有返回文本结果。");

	for (const [index, chunk] of chunks.entries()) {
		const idempotencyKey = createHash("sha256").update(`${messageId}:${index}`).digest("hex").slice(0, 40);
		const result = await execute(
			config.larkCliCommand,
			[
				"--profile",
				config.appId,
				"im",
				"+messages-send",
				"--as",
				"bot",
				"--chat-id",
				chatId,
				"--text",
				chunk,
				"--idempotency-key",
				idempotencyKey,
			],
			60_000,
		);
		const parsed: unknown = JSON.parse(result.stdout);
		if (!isRecord(parsed) || parsed.ok !== true) throw new Error("lark-cli did not confirm message delivery");
	}
}

async function runGateway(config: GatewayConfig, once: boolean, signal: AbortSignal): Promise<void> {
	await mkdir(config.stateDir, { recursive: true, mode: 0o700 });
	await chmod(config.stateDir, 0o700);
	const store = new ProcessedMessageStore(config.stateDir);
	await store.load();

	let complete: (() => void) | undefined;
	let fail: ((error: Error) => void) | undefined;
	const completion = new Promise<void>((resolveCompletion, rejectCompletion) => {
		complete = resolveCompletion;
		fail = rejectCompletion;
	});
	const pi = new PiRpcClient(config.pi, (error) => fail?.(error));
	await pi.start();

	const inFlight = new Set<string>();
	let queue = Promise.resolve();
	let stopping = false;
	const lark = spawn(
		config.larkCliCommand,
		["--profile", config.appId, "event", "consume", "im.message.receive_v1", "--as", "bot"],
		{ env: process.env, stdio: ["pipe", "pipe", "pipe"] },
	);
	const detachLarkReader = attachJsonlReader(
		lark.stdout,
		(line) => {
			const event = parseLarkEvent(line);
			if (
				!event ||
				event.chatId !== config.directChatId ||
				!config.allowFrom.has(event.senderId) ||
				store.has(event.messageId) ||
				inFlight.has(event.messageId)
			) {
				return;
			}
			inFlight.add(event.messageId);
			queue = queue
				.then(async () => {
					const startedAt = process.hrtime.bigint();
					const typingReactionId = await addTypingReaction(config, event.messageId);
					try {
						const command = event.content.trim();
						let response: string;
						if (command === "/new" && config.adminFrom.has(event.senderId)) {
							await pi.newSession();
							response = "已开始新的 Pi 会话。";
						} else if (command === "/status" && config.adminFrom.has(event.senderId)) {
							response = await pi.status();
						} else {
							response = await pi.promptAndGetText(event.content);
						}
						const elapsedSeconds = Number(process.hrtime.bigint() - startedAt) / 1_000_000_000;
						await sendLarkText(
							config,
							event.chatId,
							`${response}\n\n处理耗时：${elapsedSeconds.toFixed(1)} 秒`,
							event.messageId,
						);
						await store.add(event.messageId);
					} catch (error: unknown) {
						console.error("[gateway] message processing failed", error);
						try {
							const elapsedSeconds = Number(process.hrtime.bigint() - startedAt) / 1_000_000_000;
							await sendLarkText(
								config,
								event.chatId,
								`Pi 处理失败，请重试。\n\n处理耗时：${elapsedSeconds.toFixed(1)} 秒`,
								`${event.messageId}:error`,
							);
							await store.add(event.messageId);
						} catch (sendError: unknown) {
							console.error("[gateway] failed to send error response", sendError);
						}
					} finally {
						if (typingReactionId) {
							await removeTypingReaction(config, event.messageId, typingReactionId);
						}
						inFlight.delete(event.messageId);
					}
				})
				.then(() => {
					if (once) complete?.();
				})
				.catch((error: unknown) => fail?.(error instanceof Error ? error : new Error(String(error))));
		},
		(error) => fail?.(error),
	);
	lark.stderr.on("data", (chunk: Buffer | string) => process.stderr.write(`[lark] ${chunk.toString()}`));
	lark.once("error", (error) => fail?.(new Error(`Failed to start lark-cli: ${error.message}`)));
	lark.once("exit", (code, exitSignal) => {
		if (!stopping) fail?.(new Error(`lark-cli event consumer exited (code=${code}, signal=${exitSignal})`));
	});

	const abort = (): void => complete?.();
	if (signal.aborted) abort();
	else signal.addEventListener("abort", abort, { once: true });
	console.error("[gateway] ready; accepting one authorized Feishu direct chat");

	try {
		await completion;
		await queue;
	} finally {
		stopping = true;
		signal.removeEventListener("abort", abort);
		detachLarkReader();
		if (lark.exitCode === null) lark.kill("SIGTERM");
		await pi.stop();
	}
}

function parseCliOptions(args: string[]): CliOptions {
	let configPath = "~/.lark-cli/pi-gateway.json";
	let once = false;
	let validateOnly = false;
	for (let index = 0; index < args.length; index++) {
		const arg = args[index];
		if (arg === "--config" && args[index + 1]) {
			configPath = args[++index];
		} else if (arg === "--once") {
			once = true;
		} else if (arg === "--validate") {
			validateOnly = true;
		} else if (arg === "--help" || arg === "-h") {
			console.log("Usage: pi-feishu-gateway [--config <path>] [--once] [--validate]");
			process.exit(0);
		} else {
			throw new Error(`Unknown argument: ${arg}`);
		}
	}
	return { configPath, once, validateOnly };
}

async function main(): Promise<void> {
	const options = parseCliOptions(process.argv.slice(2));
	const config = await loadConfig(options.configPath);
	if (options.validateOnly) {
		console.log("Configuration is valid.");
		return;
	}

	const controller = new AbortController();
	const stop = (): void => controller.abort();
	process.once("SIGINT", stop);
	process.once("SIGTERM", stop);
	try {
		await runGateway(config, options.once, controller.signal);
	} finally {
		process.off("SIGINT", stop);
		process.off("SIGTERM", stop);
	}
}

main().catch((error: unknown) => {
	console.error(error instanceof Error ? error.message : String(error));
	process.exitCode = 1;
});
