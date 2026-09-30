/**
 * TokenHub v3 — opencode 插件（本地控制 API + 额度条生命周期托管）
 *
 * 约束（spec §5 / plan Task C）：
 *  - 仅使用 node:* 内建模块；不依赖 Bun.* / $ / 任何 npm 包。
 *  - 纯可擦除 TS（无 enum/namespace/参数属性），node v24 类型剥离可直接 import。
 *  - 模块顶层无副作用（全部逻辑在插件函数内 try/catch），加载失败绝不影响 opencode 启动。
 *
 * 加载语义（v1.18.32 server bundle 实证）：
 *  - PluginLoader.load → `await import(row.entry)`（file:// URL，Node 原生类型剥离）。
 *  - applyPlugin：先试 v2（mod.default 对象含 server()，文件插件须带 id）；
 *    否则 legacy 路径遍历模块【全部】命名导出，每个导出值必须本身是函数，
 *    以 fn(input, options) 调用，返回值收集为 hooks（dispose/config/event 会被调用）。
 *  → 本文件只能有【一个】导出；导出第二个函数会导致插件体被执行两次（双 server/双 spawn）。
 */

import { spawn, type ChildProcess } from "node:child_process";
import {
  createServer,
  type Server,
  type IncomingMessage,
  type ServerResponse,
} from "node:http";
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  renameSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { basename, dirname, join } from "node:path";
import { homedir } from "node:os";
import { randomBytes } from "node:crypto";

const VERSION = "3.0.0";
const AUTH_FIELD = "opencode-go";
const KEY_PREFIXES = ["sk-", "oc_sk_"] as const;

/** 密钥合法性：sk- 或 oc_sk_ 开头均有效（新旧 opencode 密钥格式兼容）。 */
function isValidKey(key: string): boolean {
  return KEY_PREFIXES.some((p) => key.startsWith(p));
}
const MAX_BODY_BYTES = 4096;
const BACKUP_KEEP = 5;

type PluginInput = {
  serverUrl?: URL | string;
  directory?: string;
  [key: string]: unknown;
};

type SwitchResult = { ok: boolean; method: "api" | "file"; error?: string };

/** 掩码（统一规则，与 Python config.mask_key 一致）：
 * oc_sk_ 开头且长度≥16 → 前8+…+后4；其他且长度≥12 → 前6+…+后4；否则 → *** */
function maskKey(key: string): string {
  if (key.startsWith("oc_sk_") && key.length >= 16) {
    return key.slice(0, 8) + "…" + key.slice(-4);
  }
  if (key.length >= 12) {
    return key.slice(0, 6) + "…" + key.slice(-4);
  }
  return "***";
}

/**
 * exe 路径解析（TH-06）：TOKENHUB_EXE 显式覆盖 → 正式安装目录
 * （%LOCALAPPDATA%\TokenHub）→ 开发目录（仅当安装缺失时回退）。
 * 正式安装优先于开发产物，避免开发目录存在时误用旧版本。
 */
function resolveExePath(): string | null {
  const candidates: (string | undefined)[] = [
    process.env.TOKENHUB_EXE,
    process.env.LOCALAPPDATA
      ? join(process.env.LOCALAPPDATA, "TokenHub", "TokenHub.exe")
      : undefined,
    "C:\\Study\\opencode\\tokenhub-v3\\dist\\TokenHub.exe",
  ];
  for (const c of candidates) {
    if (!c) continue;
    try {
      if (existsSync(c)) return c;
    } catch {
      /* ignore */
    }
  }
  return null;
}

/** 读取 JSON 文件并容忍 UTF-8 BOM（外部工具如 PowerShell 可能写入 BOM）。 */
function readJsonFile(path: string): unknown {
  let raw = readFileSync(path, "utf8");
  if (raw.charCodeAt(0) === 0xfeff) raw = raw.slice(1);
  return JSON.parse(raw);
}

/**
 * auth.json 路径解析（spec §3.2）：
 * OPENCODE_AUTH_PATH(测试) → XDG_DATA_HOME → ~/.local/share → ~/.opencode；
 * 优先选择"存在且包含 opencode-go 字段"的文件；都不存在则用第 1 项。
 */
function resolveAuthPath(): string {
  const candidates: string[] = [];
  const envPath = process.env.OPENCODE_AUTH_PATH;
  if (envPath) candidates.push(envPath);
  const xdg = process.env.XDG_DATA_HOME;
  if (xdg) candidates.push(join(xdg, "opencode", "auth.json"));
  const home = homedir();
  candidates.push(join(home, ".local", "share", "opencode", "auth.json"));
  candidates.push(join(home, ".opencode", "auth.json"));

  const existing: string[] = [];
  for (const c of candidates) {
    try {
      if (existsSync(c)) existing.push(c);
    } catch {
      /* ignore */
    }
  }
  if (existing.length === 0) return candidates[0];
  for (const c of existing) {
    try {
      const parsed = readJsonFile(c);
      if (
        parsed &&
        typeof parsed === "object" &&
        !Array.isArray(parsed) &&
        (parsed as Record<string, unknown>)[AUTH_FIELD]
      ) {
        return c;
      }
    } catch {
      /* ignore */
    }
  }
  return existing[0];
}

/** 备份保留最近 N 份，清理更旧的 */
function pruneBackups(target: string, keep: number): void {
  try {
    const dir = dirname(target);
    const prefix = basename(target) + ".tokenhub.bak-";
    const entries = readdirSync(dir)
      .filter((n) => n.startsWith(prefix))
      .sort()
      .reverse();
    for (const old of entries.slice(keep)) {
      try {
        rmSync(join(dir, old), { force: true });
      } catch {
        /* ignore */
      }
    }
  } catch {
    /* ignore */
  }
}

/**
 * 恢复到变更前状态（R3-01/R3-02/R2-04 镜像，与 Python 端 _rollback 语义一致）。
 * - R3-01：仅在原文件本不存在（existed=false）时允许删除新文件；
 *   原文件存在而备份缺失 → 保留现状，返回失败。
 * - R3-02：恢复成功判定 = 恢复后内容与备份一致（字节级 Buffer.equals），
 *   不依赖原文件是否含 opencode-go 字段；备份不可解析/恢复写入失败明确失败。
 * - R2-04：恢复永不消耗备份（copyFileSync + 同目录临时文件 + renameSync）。
 * 任何失败返回 ok:false 并附与文件实际状态一致的提示（引用备份路径前核对存在性）。
 */
function tryRestoreAuthFile(
  target: string,
  backupPath: string | null,
  existed: boolean,
): { ok: boolean; hint?: string } {
  const failMsg = (): string =>
    existsSync(backupPath ?? "")
      ? `恢复失败，状态待确认；备份位于 ${backupPath}`
      : "恢复失败，状态待确认";
  if (backupPath && existsSync(backupPath)) {
    let backupBytes: Buffer;
    try {
      backupBytes = readFileSync(backupPath);
    } catch {
      return { ok: false, hint: failMsg() };
    }
    // 备份必须可解析（不可解析 → 明确失败，不判成功）
    try {
      JSON.parse(backupBytes.toString("utf8").replace(/^\uFEFF/, ""));
    } catch {
      return { ok: false, hint: failMsg() };
    }
    const tmp = `${target}.tokenhub.restoretmp-${randomBytes(4).toString("hex")}`;
    try {
      copyFileSync(backupPath, tmp);
      renameSync(tmp, target);
    } catch (e) {
      try {
        rmSync(tmp, { force: true });
      } catch {
        /* ignore */
      }
      return {
        ok: false,
        hint: existsSync(backupPath)
          ? `恢复失败，状态待确认；备份位于 ${backupPath}`
          : `恢复失败，状态待确认（${e instanceof Error ? e.message : String(e)}）`,
      };
    }
    try {
      const nowBytes = readFileSync(target);
      if (nowBytes.equals(backupBytes)) {
        return { ok: true };
      }
    } catch {
      /* fallthrough */
    }
    return { ok: false, hint: failMsg() };
  }
  if (!existed) {
    try {
      rmSync(target, { force: true });
      if (!existsSync(target)) return { ok: true };
    } catch {
      /* fallthrough */
    }
    return { ok: false, hint: "恢复失败，状态待确认" };
  }
  return { ok: false, hint: "恢复失败，状态待确认" };
}

/**
 * 直写 auth.json（spec §3.2）：备份 + 临时文件 + rename 原子替换 + 读回校验 + 失败回滚。
 * 仅修改 opencode-go 字段，保留其他键。
 */
function writeAuthFile(key: string): SwitchResult {
  const target = resolveAuthPath();
  const dir = dirname(target);
  let data: Record<string, unknown> = {};
  let existed = false;
  let backupPath: string | null = null;

  try {
    if (existsSync(target)) {
      existed = true;
      const parsed = readJsonFile(target);
      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
        return { ok: false, method: "file", error: "auth.json 顶层不是对象，拒绝覆盖" };
      }
      data = parsed as Record<string, unknown>;
    }
  } catch (e) {
    return {
      ok: false,
      method: "file",
      error: `读取 auth.json 失败: ${e instanceof Error ? e.message : String(e)}`,
    };
  }

  try {
    if (existed) {
      const ts = new Date().toISOString().replace(/[:.]/g, "-");
      backupPath = `${target}.tokenhub.bak-${ts}`;
      copyFileSync(target, backupPath);
      pruneBackups(target, BACKUP_KEEP);
    } else {
      mkdirSync(dir, { recursive: true });
    }
  } catch (e) {
    return {
      ok: false,
      method: "file",
      error: `备份 auth.json 失败: ${e instanceof Error ? e.message : String(e)}`,
    };
  }

  data[AUTH_FIELD] = { type: "api", key };

  const tmp = `${target}.tokenhub.tmp-${randomBytes(4).toString("hex")}`;
  try {
    writeFileSync(tmp, JSON.stringify(data, null, 2), "utf8");
    renameSync(tmp, target);
  } catch (e) {
    try {
      rmSync(tmp, { force: true });
    } catch {
      /* ignore */
    }
    // TH-03：原子替换失败 = 目标文件未被触碰，原账号未受影响
    return {
      ok: false,
      method: "file",
      error: `写入 auth.json 失败，原账号未受影响: ${e instanceof Error ? e.message : String(e)}`,
    };
  }

  // 读回校验；失败回滚（TH-03：如实报告恢复结果）
  try {
    const back = readJsonFile(target) as Record<string, unknown>;
    const got = back?.[AUTH_FIELD] as { type?: unknown; key?: unknown } | null;
    if (!got || got.type !== "api" || got.key !== key) {
      throw new Error("读回校验失败：opencode-go 字段与写入不一致");
    }
  } catch (e) {
    const restore = tryRestoreAuthFile(target, backupPath, existed);
    const detail = e instanceof Error ? e.message : String(e);
    if (restore.ok) {
      return {
        ok: false,
        method: "file",
        error: `校验 auth.json 失败，已恢复原账号: ${detail}`,
      };
    }
    return {
      ok: false,
      method: "file",
      error: `校验 auth.json 失败，状态待确认（${restore.hint ?? "无可用备份"}）: ${detail}`,
    };
  }
  return { ok: true, method: "file" };
}

/**
 * 官方切换通道：PUT {serverUrl}auth/opencode-go，payload {type:"api",key}（F5/C2 实证）。
 * Basic Auth 取 env OPENCODE_SERVER_USERNAME/OPENCODE_SERVER_PASSWORD（无密码则不带 header）。
 * 3s 超时；任何非 2xx / 网络错误均返回 false（调用方降级直写文件）。
 */
async function apiSwitch(serverUrl: URL | string | undefined, key: string): Promise<boolean> {
  if (!serverUrl) return false;
  try {
    const base = new URL(serverUrl.toString());
    base.pathname = "/";
    base.search = "";
    base.hash = "";
    const url = new URL(`auth/${AUTH_FIELD}`, base);
    const headers: Record<string, string> = { "content-type": "application/json" };
    const username = process.env.OPENCODE_SERVER_USERNAME ?? "opencode";
    const password = process.env.OPENCODE_SERVER_PASSWORD;
    if (password) {
      headers.authorization = `Basic ${Buffer.from(`${username}:${password}`).toString("base64")}`;
    }
    const res = await fetch(url, {
      method: "PUT",
      headers,
      body: JSON.stringify({ type: "api", key }),
      signal: AbortSignal.timeout(3000),
    });
    return res.ok;
  } catch {
    return false;
  }
}

function sendJson(res: ServerResponse, code: number, obj: unknown): void {
  try {
    const payload = JSON.stringify(obj);
    res.writeHead(code, {
      "content-type": "application/json; charset=utf-8",
      "content-length": Buffer.byteLength(payload),
    });
    res.end(payload);
  } catch {
    /* ignore */
  }
}

/** POST /switch：校验 token + key（sk- 或 oc_sk_ 开头）→ API 优先 → 文件兜底 */
function handleSwitch(req: IncomingMessage, res: ServerResponse, input: PluginInput): void {
  let raw = "";
  let totalBytes = 0;
  let overflow = false;
  // §7.8：按字节累计；超限立即停止拼接（丢弃后续数据），end 时明确拒绝 413
  req.on("data", (chunk: Buffer) => {
    totalBytes += chunk.length;
    if (totalBytes > MAX_BODY_BYTES) {
      overflow = true;
      return;
    }
    raw += chunk.toString("utf8");
  });
  req.on("end", () => {
    void (async () => {
      if (overflow) {
        sendJson(res, 413, { ok: false, error: "body too large" });
        return;
      }
      let body: { key?: unknown };
      try {
        body = JSON.parse(raw || "{}") as { key?: unknown };
      } catch {
        sendJson(res, 400, { ok: false, error: "invalid JSON" });
        return;
      }
      const key = body?.key;
      if (typeof key !== "string" || !isValidKey(key)) {
        sendJson(res, 400, { ok: false, error: "key 必须以 sk- 或 oc_sk_ 开头" });
        return;
      }
      const ok = await apiSwitch(input.serverUrl, key);
      if (ok) {
        sendJson(res, 200, { ok: true, method: "api" });
        return;
      }
      const result = writeAuthFile(key);
      if (result.ok) {
        sendJson(res, 200, { ok: true, method: "file" });
      } else {
        sendJson(res, 200, {
          ok: false,
          method: "file",
          error: result.error ?? "未知错误",
        });
      }
    })().catch((e) => {
      sendJson(res, 500, {
        ok: false,
        error: `switch failed: ${e instanceof Error ? e.message : String(e)}`,
      });
    });
  });
  req.on("error", () => {
    /* ignore */
  });
}

/** 本地控制 HTTP server（127.0.0.1 随机端口，随机 token） */
function startControlServer(
  input: PluginInput,
): Promise<{ server: Server; port: number; token: string }> {
  return new Promise((resolve, reject) => {
    const token = randomBytes(16).toString("hex");
    const server = createServer((req, res) => {
      if (req.headers["x-tokenhub-token"] !== token) {
        sendJson(res, 401, { ok: false, error: "invalid token" });
        return;
      }
      const url = req.url ?? "/";
      if (req.method === "GET" && url === "/health") {
        sendJson(res, 200, { ok: true, version: VERSION });
        return;
      }
      if (req.method === "POST" && url === "/switch") {
        handleSwitch(req, res, input);
        return;
      }
      sendJson(res, 404, { ok: false, error: "not found" });
    });
    server.on("error", (e) => reject(e));
    server.listen(0, "127.0.0.1", () => {
      const addr = server.address();
      if (!addr || typeof addr === "string") {
        reject(new Error("无法获取控制端口"));
        return;
      }
      resolve({ server, port: addr.port, token });
    });
  });
}

/**
 * 插件入口（v1.18.32 legacy 形态：唯一命名导出，async (input) => hooks）。
 * 全部逻辑 try/catch：任何失败仅 console.error("[tokenhub] ...") 并返回空 hooks。
 */
export const TokenHub = async (input: PluginInput): Promise<Record<string, unknown>> => {
  try {
    const { server, port, token } = await startControlServer(input);
    let child: ChildProcess | null = null;

    if (process.env.TOKENHUB_DISABLE_SPAWN === "1") {
      console.log("[tokenhub] TOKENHUB_DISABLE_SPAWN=1，跳过 spawn");
    } else {
      const exe = resolveExePath();
      if (!exe) {
        console.log("[tokenhub] 未找到 TokenHub.exe，跳过 spawn（安全：额度条可独立运行）");
      } else {
        try {
          child = spawn(exe, [], {
            env: {
              ...process.env,
              TOKENHUB_MANAGED: "1",
              TOKENHUB_CONTROL_PORT: String(port),
              TOKENHUB_CONTROL_TOKEN: token,
              TOKENHUB_PARENT_PID: String(process.pid),
            },
            stdio: "ignore",
            windowsHide: true,
          });
          child.on("error", (e) => {
            console.error(`[tokenhub] spawn TokenHub.exe 失败: ${e.message}`);
            child = null;
          });
          console.log(`[tokenhub] 已启动 TokenHub.exe（控制端口 ${port}）`);
        } catch (e) {
          console.error(
            `[tokenhub] spawn TokenHub.exe 失败: ${e instanceof Error ? e.message : String(e)}`,
          );
          child = null;
        }
      }
    }

    const hooks: Record<string, unknown> = {
      _tokenhubControl: { port, token },
      // TH-06/TH-03：暴露辅助函数供冒烟测试断言（不新增命名导出，
      // 保持 legacy 加载器"仅一个命名导出"的约束）
      _tokenhubResolveExe: resolveExePath,
      _tokenhubRestoreAuthFile: tryRestoreAuthFile,
      dispose: () => {
        try {
          server.close();
        } catch (e) {
          console.error(
            `[tokenhub] 关闭控制服务器失败: ${e instanceof Error ? e.message : String(e)}`,
          );
        }
        if (child) {
          try {
            child.kill();
          } catch (e) {
            console.error(
              `[tokenhub] 终止 TokenHub.exe 失败: ${e instanceof Error ? e.message : String(e)}`,
            );
          }
        }
        console.log("[tokenhub] disposed");
      },
    };

    console.log(
      `[tokenhub] 控制 API 就绪: http://127.0.0.1:${port} token=${token.slice(0, 4)}…（密钥不出现在日志）`,
    );
    return hooks;
  } catch (e) {
    console.error(
      `[tokenhub] 初始化失败（不影响 opencode 启动）: ${e instanceof Error ? e.message : String(e)}`,
    );
    return {};
  }
};