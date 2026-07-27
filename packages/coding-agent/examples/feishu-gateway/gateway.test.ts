import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { chmod, mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const gatewayPath = join(dirname(fileURLToPath(import.meta.url)), "gateway.ts");

async function writeExecutable(path: string, content: string): Promise<void> {
	await writeFile(path, content, { mode: 0o700 });
	await chmod(path, 0o700);
}

function runGateway(configPath: string, env: NodeJS.ProcessEnv): Promise<{ stdout: string; stderr: string }> {
	return new Promise((resolveRun, rejectRun) => {
		const child = spawn(process.execPath, [gatewayPath, "--config", configPath, "--once"], {
			env,
			stdio: ["ignore", "pipe", "pipe"],
		});
		let stdout = "";
		let stderr = "";
		child.stdout.on("data", (chunk: Buffer | string) => {
			stdout += chunk.toString();
		});
		child.stderr.on("data", (chunk: Buffer | string) => {
			stderr += chunk.toString();
		});
		const timer = setTimeout(() => {
			child.kill("SIGKILL");
			rejectRun(new Error(`gateway timed out\n${stderr}`));
		}, 10_000);
		child.once("error", (error) => {
			clearTimeout(timer);
			rejectRun(error);
		});
		child.once("exit", (code, signal) => {
			clearTimeout(timer);
			if (code === 0) resolveRun({ stdout, stderr });
			else rejectRun(new Error(`gateway exited code=${code} signal=${signal}\n${stderr}`));
		});
	});
}

test("bridges only the authorized direct chat through Pi RPC", async () => {
	const directory = await mkdtemp(join(tmpdir(), "pi-feishu-gateway-test-"));
	const fakePiPath = join(directory, "fake-pi.mjs");
	const fakeLarkPath = join(directory, "fake-lark.mjs");
	const sendLogPath = join(directory, "send.ndjson");
	const configPath = join(directory, "config.json");
	const stateDir = join(directory, "state");

	await writeExecutable(
		fakePiPath,
		`#!/usr/bin/env node
let buffer = "";
let lastText = null;
function send(value) { process.stdout.write(JSON.stringify(value) + "\\n"); }
process.stdin.on("data", (chunk) => {
  buffer += chunk.toString();
  let index = buffer.indexOf("\\n");
  while (index !== -1) {
    const line = buffer.slice(0, index);
    buffer = buffer.slice(index + 1);
    const command = JSON.parse(line);
    if (command.type === "get_state") {
      send({type:"response", id:command.id, command:command.type, success:true, data:{model:{provider:"fake",id:"test"},sessionName:"Feishu test",messageCount:1}});
    } else if (command.type === "prompt") {
      lastText = "Pi reply: " + command.message;
      send({type:"response", id:command.id, command:command.type, success:true});
      send({type:"agent_settled"});
    } else if (command.type === "get_last_assistant_text") {
      send({type:"response", id:command.id, command:command.type, success:true, data:{text:lastText}});
    } else {
      send({type:"response", id:command.id, command:command.type, success:true, data:{cancelled:false}});
    }
    index = buffer.indexOf("\\n");
  }
});
`,
	);

	await writeExecutable(
		fakeLarkPath,
		`#!/usr/bin/env node
import fs from "node:fs";
const args = process.argv.slice(2);
function log(kind) { fs.appendFileSync(process.env.SEND_LOG, JSON.stringify({kind,args}) + "\\n"); }
if (args.includes("consume")) {
  process.stdout.write(JSON.stringify({type:"im.message.receive_v1",chat_id:"oc_other",chat_type:"p2p",content:"unauthorized",message_id:"om_bad",sender_id:"ou_other",sender_type:"user"}) + "\\n");
  process.stdout.write(JSON.stringify({type:"im.message.receive_v1",chat_id:"oc_allowed",chat_type:"p2p",content:"hello\\u2028world",message_id:"om_allowed",sender_id:"ou_allowed",sender_type:"user"}) + "\\n");
  setInterval(() => {}, 1000);
} else if (args.includes("+messages-send")) {
  log("send");
  process.stdout.write(JSON.stringify({ok:true,data:{message_id:"om_reply"}}) + "\\n");
} else if (args.includes("reactions") && args.includes("create")) {
  log("typing-create");
  process.stdout.write(JSON.stringify({ok:true,data:{reaction_id:"reaction_typing"}}) + "\\n");
} else if (args.includes("reactions") && args.includes("delete")) {
  log("typing-delete");
  process.stdout.write(JSON.stringify({ok:true,data:{reaction_id:"reaction_typing"}}) + "\\n");
} else {
  process.exitCode = 2;
}
`,
	);

	await writeFile(
		configPath,
		`${JSON.stringify(
			{
				app_id: "cli_test",
				allow_from: ["ou_allowed"],
				admin_from: ["ou_allowed"],
				direct_chat_id: "oc_allowed",
				state_dir: stateDir,
				lark_cli_command: fakeLarkPath,
				pi: {
					command: fakePiPath,
					cwd: directory,
					session_dir: join(directory, "sessions"),
					session_id: "test-session",
					session_name: "Feishu test",
					response_timeout_ms: 5000,
				},
			},
			null,
			2,
		)}\n`,
		{ mode: 0o600 },
	);
	await chmod(configPath, 0o600);

	const result = await runGateway(configPath, { ...process.env, SEND_LOG: sendLogPath });
	assert.match(result.stderr, /gateway\] ready/);
	assert.equal(result.stdout, "");

	const operations = (await readFile(sendLogPath, "utf8"))
		.trim()
		.split("\n")
		.map((line) => JSON.parse(line) as { kind: string; args: string[] });
	assert.deepEqual(
		operations.map((operation) => operation.kind),
		["typing-create", "send", "typing-delete"],
	);
	const create = operations[0].args;
	assert.equal(JSON.parse(create[create.indexOf("--data") + 1]).reaction_type.emoji_type, "Typing");
	const send = operations[1].args;
	assert.equal(send[send.indexOf("--chat-id") + 1], "oc_allowed");
	const sentText = send[send.indexOf("--text") + 1];
	assert.equal(sentText.startsWith("Pi reply: hello\u2028world\n\n"), true);
	assert.match(sentText, /处理耗时：\d+\.\d 秒$/);
	const remove = operations[2].args;
	assert.deepEqual(JSON.parse(remove[remove.indexOf("--params") + 1]), {
		message_id: "om_allowed",
		reaction_id: "reaction_typing",
	});

	const processed = JSON.parse(await readFile(join(stateDir, "processed-message-ids.json"), "utf8")) as string[];
	assert.deepEqual(processed, ["om_allowed"]);
});
