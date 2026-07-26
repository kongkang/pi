/**
 * PyAgent 授信守卫：在工具执行前拦截需要用户知情的操作，升级给用户裁决。
 *
 * ⚠ 定位必须说清楚：**这不是安全边界，是提醒层。**
 *
 * 实测证据：本守卫拦掉 `rm -rf <file>` 之后，模型立刻改用
 * `python -c "os.remove(...)"` 并成功删除了目标文件。基于命令文本的黑名单
 * 不可能穷尽 —— 任何解释器（python/node/perl/ruby）都能达成同样效果。
 * pi 官方 security.md 也明确讲过：进程内的部分沙箱容易被误当成安全边界。
 *
 * 真正的写入边界由 pyagent/src/pyagent/sandbox.py 用 macOS 内核沙箱
 * （sandbox-exec）强制，默认开启。同一个 python 绕过手法在内核沙箱下会拿到
 * PermissionError —— 这才是拦得住的那一层。
 *
 * 那本守卫的价值是什么：表达"这操作要不要问人"。内核沙箱只能表达能不能写，
 * 无法表达 `git push`、`npm publish` 这类"权限上允许但你应该知情"的操作。
 * 两层职责不同，都需要。
 *
 * 在 RPC 模式下，ctx.ui.select() 会变成一条 extension_ui_request 发给 PyAgent，
 * 由它转成飞书卡片按钮。用户点 Allow 才继续，超时/无人应答一律按 Block。
 *
 * 用 `-e` 显式加载，因此不受项目信任开关影响 —— 守卫必须永远生效。
 */

import { isAbsolute, resolve } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

/** 一次越界判定的结果。 */
type Verdict =
	| { escalate: false }
	| { escalate: true; title: string; detail: string; reason: string };

/** 高危 shell 模式：命中即升级，不看路径。 */
const DANGEROUS_PATTERNS: ReadonlyArray<{ re: RegExp; what: string }> = [
	{ re: /\brm\s+-[a-zA-Z]*[rf]/, what: "递归/强制删除文件" },
	{ re: /\bgit\s+push\b/, what: "推送到远端仓库" },
	{ re: /\bgit\s+reset\s+--hard\b/, what: "丢弃本地改动" },
	{ re: /\bgit\s+clean\s+-[a-zA-Z]*[fd]/, what: "清除未跟踪文件" },
	{ re: /\bsudo\b/, what: "以管理员权限执行" },
	{ re: /\b(curl|wget)\b[^|]*\|\s*(ba)?sh\b/, what: "下载并直接执行脚本" },
	{ re: /\bnpm\s+publish\b|\bpnpm\s+publish\b|\byarn\s+publish\b/, what: "发布 npm 包" },
	{ re: /\b(shutdown|reboot|halt)\b/, what: "关机/重启" },
	{ re: />\s*\/dev\/(sd|disk|nvme)/, what: "直接写块设备" },
	{ re: /\bchmod\s+-R\s+777\b/, what: "放开全局权限" },
];

/** 读到这些路径要当心：凭证与私钥。 */
const SENSITIVE_PATHS: ReadonlyArray<{ re: RegExp; what: string }> = [
	{ re: /\/\.ssh\//, what: "SSH 私钥目录" },
	{ re: /\/\.aws\//, what: "AWS 凭证" },
	{ re: /\/\.codex\/auth\.json$/, what: "Codex 凭证" },
	{ re: /\/\.pi\/agent\/auth\.json$/, what: "pi 凭证" },
	{ re: /\/\.netrc$/, what: "netrc 凭证" },
	{ re: /\/\.env(\.|$)/, what: "环境变量文件" },
];

/** 目标路径是否落在工作目录之内。 */
function insideCwd(target: string, cwd: string): boolean {
	const abs = isAbsolute(target) ? target : resolve(cwd, target);
	const root = cwd.endsWith("/") ? cwd : `${cwd}/`;
	return abs === cwd || abs.startsWith(root);
}

/** 从工具入参里尽力找出它要碰的文件路径。 */
function extractPaths(input: Record<string, unknown>): string[] {
	const keys = ["path", "file_path", "filePath", "target", "dest", "destination"];
	const found: string[] = [];
	for (const k of keys) {
		const v = input[k];
		if (typeof v === "string" && v.length > 0) found.push(v);
	}
	return found;
}

function assess(toolName: string, input: Record<string, unknown>, cwd: string): Verdict {
	// ── shell 命令 ──
	if (toolName === "bash") {
		const command = typeof input.command === "string" ? input.command : "";
		if (!command) return { escalate: false };

		for (const { re, what } of DANGEROUS_PATTERNS) {
			if (re.test(command)) {
				return {
					escalate: true,
					title: `高危命令：${what}`,
					detail: "```\n" + command.slice(0, 600) + "\n```",
					reason: `被 PyAgent 守卫拦截：${what}`,
				};
			}
		}

		for (const { re, what } of SENSITIVE_PATHS) {
			if (re.test(command)) {
				return {
					escalate: true,
					title: `触碰敏感文件：${what}`,
					detail: "```\n" + command.slice(0, 600) + "\n```",
					reason: `被 PyAgent 守卫拦截：命令涉及${what}`,
				};
			}
		}
		return { escalate: false };
	}

	// ── 文件写入类 ──
	if (toolName === "write" || toolName === "edit" || toolName === "multi_edit") {
		for (const p of extractPaths(input)) {
			for (const { re, what } of SENSITIVE_PATHS) {
				if (re.test(p)) {
					return {
						escalate: true,
						title: `写入敏感文件：${what}`,
						detail: `目标：\`${p}\``,
						reason: `被 PyAgent 守卫拦截：写入${what}`,
					};
				}
			}
			if (!insideCwd(p, cwd)) {
				return {
					escalate: true,
					title: "写入当前项目之外的文件",
					detail: `目标：\`${p}\`\n项目目录：\`${cwd}\``,
					reason: "被 PyAgent 守卫拦截：跨项目写入",
				};
			}
		}
	}

	return { escalate: false };
}

export default function (pi: ExtensionAPI) {
	pi.on("tool_call", async (event, ctx) => {
		const input = (event.input ?? {}) as Record<string, unknown>;
		const verdict = assess(event.toolName, input, ctx.cwd);
		if (!verdict.escalate) return;

		// 在 RPC 模式下这会成为一条 extension_ui_request，由 PyAgent 转飞书卡片。
		// 返回 undefined（用户未选/超时/无处理器）时走下面的 Block 分支 —— 安全默认。
		const choice = await ctx.ui.select(`${verdict.title}\n${verdict.detail}`, [
			"Allow",
			"Block",
		]);

		if (choice !== "Allow") {
			ctx.ui.notify(verdict.reason, "warning");
			return { block: true, reason: verdict.reason };
		}
	});
}
