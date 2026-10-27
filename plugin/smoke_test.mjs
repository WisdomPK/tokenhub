/**
 * TokenHub v3 插件冒烟测试（Node >= 24，node plugin/smoke_test.mjs）
 *
 * 覆盖：
 *  1. /health 正确 token → 200 {ok:true, version}
 *  2. /health、/switch 错误/缺失 token → 401
 *  3. /switch 正确 token + 不可达 serverUrl → API 分支失败 → 文件分支：
 *     tmp auth.json 的 opencode-go.key 变为新值，other 字段保留
 *  4. /switch 非 sk- key → 400
 *  5. dispose() 后控制端口不可再连
 *
 * 环境：TOKENHUB_DISABLE_SPAWN=1（不 spawn 假 exe）；OPENCODE_AUTH_PATH=<临时文件>。
 */
import { strict as assert } from "node:assert";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const tmp = mkdtempSync(join(tmpdir(), "tokenhub-smoke-"));
const authPath = join(tmp, "auth.json");
writeFileSync(
  authPath,
  JSON.stringify(
    { "opencode-go": { type: "api", key: "sk-old" }, other: { type: "api", key: "x" } },
    null,
    2,
  ),
);

process.env.TOKENHUB_DISABLE_SPAWN = "1";
process.env.OPENCODE_AUTH_PATH = authPath;

const { TokenHub } = await import(pathToFileURL(join(process.cwd(), "plugin", "tokenhub.ts")).href);

const input = {
  serverUrl: new URL("http://127.0.0.1:1/"), // 不可达 → 走文件分支
  directory: tmp,
};

const hooks = await TokenHub(input);
const ctrl = hooks._tokenhubControl;
assert.ok(ctrl && typeof ctrl.port === "number" && ctrl.port > 0, "应返回控制端口");
assert.ok(typeof ctrl.token === "string" && ctrl.token.length === 32, "token 应为 32 hex");
const base = `http://127.0.0.1:${ctrl.port}`;

// 1. /health 正确 token → 200
let res = await fetch(`${base}/health`, { headers: { "X-TokenHub-Token": ctrl.token } });
assert.equal(res.status, 200, "health 应 200");
const health = await res.json();
assert.equal(health.ok, true);
assert.ok(typeof health.version === "string");

// 2. 错误/缺失 token → 401
res = await fetch(`${base}/health`, { headers: { "X-TokenHub-Token": "wrong" } });
assert.equal(res.status, 401, "health 错 token 应 401");
res = await fetch(`${base}/health`);
assert.equal(res.status, 401, "health 无 token 应 401");
res = await fetch(`${base}/switch`, {
  method: "POST",
  headers: { "X-TokenHub-Token": "wrong", "content-type": "application/json" },
  body: JSON.stringify({ key: "sk-new" }),
});
assert.equal(res.status, 401, "switch 错 token 应 401");

// 3. 正确 token + 不可达 serverUrl → 文件分支
res = await fetch(`${base}/switch`, {
  method: "POST",
  headers: { "X-TokenHub-Token": ctrl.token, "content-type": "application/json" },
  body: JSON.stringify({ key: "sk-new-key-1234" }),
});
assert.equal(res.status, 200, "switch 应 200");
const sw = await res.json();
assert.equal(sw.ok, true);
assert.equal(sw.method, "file", "不可达 serverUrl 应走文件分支");
const auth = JSON.parse(readFileSync(authPath, "utf8"));
assert.equal(auth["opencode-go"].key, "sk-new-key-1234", "opencode-go.key 应更新");
assert.equal(auth["opencode-go"].type, "api");
assert.equal(auth.other.key, "x", "other 字段应保留");

// 3b. oc_sk_ 前缀 key（新版 opencode 密钥）→ 同样走文件分支成功
res = await fetch(`${base}/switch`, {
  method: "POST",
  headers: { "X-TokenHub-Token": ctrl.token, "content-type": "application/json" },
  body: JSON.stringify({ key: "oc_sk_ABCDEFGHIJKLMNOPQRSTUVwxYZ" }),
});
assert.equal(res.status, 200, "oc_sk_ key 应 200");
const swOc = await res.json();
assert.equal(swOc.ok, true);
assert.equal(swOc.method, "file", "oc_sk_ key 应走文件分支");
const authOc = JSON.parse(readFileSync(authPath, "utf8"));
assert.equal(authOc["opencode-go"].key, "oc_sk_ABCDEFGHIJKLMNOPQRSTUVwxYZ", "opencode-go.key 应更新为 oc_sk_ 值");
assert.equal(authOc["opencode-go"].type, "api");
assert.equal(authOc.other.key, "x", "other 字段应保留");

// 3c. UTF-8 BOM 容忍：外部工具写入 BOM 的 auth.json，/switch 文件分支仍可读取
const bomAuth = "\uFEFF" + JSON.stringify({
  "opencode-go": { type: "api", key: "sk-bom-old" },
  other: { type: "api", key: "x" },
});
writeFileSync(authPath, bomAuth, "utf8");
res = await fetch(`${base}/switch`, {
  method: "POST",
  headers: { "X-TokenHub-Token": ctrl.token, "content-type": "application/json" },
  body: JSON.stringify({ key: "sk-bom-new-5678" }),
});
assert.equal(res.status, 200, "BOM auth.json 的 switch 应 200");
const swBom = await res.json();
assert.equal(swBom.ok, true);
assert.equal(swBom.method, "file", "BOM auth.json 应走文件分支");
const authBom = JSON.parse(readFileSync(authPath, "utf8"));
assert.equal(authBom["opencode-go"].key, "sk-bom-new-5678", "BOM 文件切换后 key 正确");
assert.equal(authBom.other.key, "x", "BOM 文件 other 字段保留");

// 4. 无效前缀（非 sk- 且非 oc_sk_）→ 400
res = await fetch(`${base}/switch`, {
  method: "POST",
  headers: { "X-TokenHub-Token": ctrl.token, "content-type": "application/json" },
  body: JSON.stringify({ key: "bad-key" }),
});
assert.equal(res.status, 400, "无效前缀 key 应 400");
// 4b. 非法 JSON → 400
res = await fetch(`${base}/switch`, {
  method: "POST",
  headers: { "X-TokenHub-Token": ctrl.token, "content-type": "application/json" },
  body: "{oops",
});
assert.equal(res.status, 400, "非法 JSON 应 400");

// 4c. §7.8：body 超限按字节拒绝（413）
res = await fetch(`${base}/switch`, {
  method: "POST",
  headers: { "X-TokenHub-Token": ctrl.token, "content-type": "application/json" },
  body: JSON.stringify({ key: "sk-" + "x".repeat(6000) }),  // > 4096 字节
});
assert.equal(res.status, 413, "超限 body 应 413");

// 5. dispose 后端口不可再连
await hooks.dispose();
let stillUp = true;
try {
  await fetch(`${base}/health`, {
    headers: { "X-TokenHub-Token": ctrl.token },
    signal: AbortSignal.timeout(2000),
  });
} catch {
  stillUp = false;
}
assert.equal(stillUp, false, "dispose 后控制端口应不可再连");

// 6. TH-06 EXE 路径顺序：TOKENHUB_EXE → 正式安装目录 → 开发目录
const resolveExe = hooks._tokenhubResolveExe;
assert.equal(typeof resolveExe, "function", "hooks 应暴露 _tokenhubResolveExe");
const devExe = fileURLToPath(new URL("../dist/TokenHub.exe", import.meta.url));
// 6a. TOKENHUB_EXE 显式覆盖最优先
const fakeExplicit = join(tmp, "explicit.exe");
writeFileSync(fakeExplicit, "");
process.env.TOKENHUB_EXE = fakeExplicit;
assert.equal(resolveExe(), fakeExplicit, "TOKENHUB_EXE 应最优先");
// 6b. 无 TOKENHUB_EXE 时，正式安装目录优先于开发目录
delete process.env.TOKENHUB_EXE;
const fakeLocal = join(tmp, "local");
const fakeInstalled = join(fakeLocal, "TokenHub", "TokenHub.exe");
mkdirSync(dirname(fakeInstalled), { recursive: true });
writeFileSync(fakeInstalled, "");
const savedLocalAppData = process.env.LOCALAPPDATA;
process.env.LOCALAPPDATA = fakeLocal;
assert.equal(resolveExe(), fakeInstalled, "正式安装目录应优先于开发目录");
// 6c. 安装目录缺失时回退开发目录
rmSync(fakeLocal, { recursive: true, force: true });
assert.equal(resolveExe(), devExe, "安装缺失时应回退开发目录");
if (savedLocalAppData === undefined) delete process.env.LOCALAPPDATA;
else process.env.LOCALAPPDATA = savedLocalAppData;

// 7. TH-03/R2-04 文件降级回滚语义（tryRestoreAuthFile 直测，与 Python 端 _rollback 对齐）
const restoreFn = hooks._tokenhubRestoreAuthFile;
assert.equal(typeof restoreFn, "function", "hooks 应暴露 _tokenhubRestoreAuthFile");
// 7a. 备份存在且可恢复 → ok:true，目标恢复为备份内容；
//     R2-04：备份不被消耗（恢复后备份仍存在且内容=原始）
const rTarget = join(tmp, "restore_auth.json");
const rBackup = join(tmp, "restore_auth.json.tokenhub.bak-1");
const rBackupOriginal = JSON.stringify({ "opencode-go": { type: "api", key: "sk-old" }, other: 1 });
writeFileSync(rTarget, JSON.stringify({ "opencode-go": { type: "api", key: "sk-new" } }));
writeFileSync(rBackup, rBackupOriginal);
let r = restoreFn(rTarget, rBackup, true);
assert.equal(r.ok, true, "备份恢复应成功");
assert.equal(
  JSON.parse(readFileSync(rTarget, "utf8"))["opencode-go"].key,
  "sk-old",
  "恢复后应为备份内容",
);
assert.equal(existsSync(rBackup), true, "R2-04：恢复不得消耗备份");
assert.equal(readFileSync(rBackup, "utf8"), rBackupOriginal, "备份内容应保持原始");
// 7b. 恢复失败（备份缺失但原文件存在）→ ok:false + hint 不含"备份位于"（不得虚报路径）
rmSync(rBackup, { force: true });
r = restoreFn(rTarget, null, true);
assert.equal(r.ok, false, "无备份且原文件存在应恢复失败");
assert.ok(r.hint && r.hint.includes("待确认"), "hint 应说明状态待确认");
assert.ok(!r.hint.includes("备份位于"), "备份不存在时不得声称备份位置");
// 7c. 原文件本不存在（新文件）→ 删除即恢复
const rNew = join(tmp, "restore_new.json");
writeFileSync(rNew, JSON.stringify({ "opencode-go": { type: "api", key: "sk-new" } }));
r = restoreFn(rNew, null, false);
assert.equal(r.ok, true, "新文件删除即恢复");
assert.equal(existsSync(rNew), false, "新文件应被删除");
// 7d. R3-02：备份无 Go 字段（仅他商/空对象）→ 恢复成功（内容一致判定，不依赖 Go key）
for (const [i, content] of [
  JSON.stringify({ openai: { type: "api", key: "sk-x" } }),
  JSON.stringify({ "opencode-go": {}, deepseek: { type: "api", key: "sk-d" } }),
].entries()) {
  const rt = join(tmp, `restore_no_go_${i}.json`);
  const rb = join(tmp, `restore_no_go_${i}.json.tokenhub.bak-1`);
  writeFileSync(rt, JSON.stringify({ "opencode-go": { type: "api", key: "sk-tampered" } }));
  writeFileSync(rb, content);
  const rr = restoreFn(rt, rb, true);
  assert.equal(rr.ok, true, `无 Go 字段备份(场景 ${i})恢复应成功`);
  assert.equal(readFileSync(rt, "utf8"), content, `场景 ${i}：恢复后内容=备份`);
  assert.equal(existsSync(rb), true, `场景 ${i}：备份保留`);
}
// 7e. R3-02：备份不可解析 → 明确失败，备份保留
const rT4 = join(tmp, "restore_broken.json");
const rB4 = join(tmp, "restore_broken.json.tokenhub.bak-1");
writeFileSync(rT4, JSON.stringify({ "opencode-go": { type: "api", key: "sk-tampered" } }));
writeFileSync(rB4, "{broken json");
r = restoreFn(rT4, rB4, true);
assert.equal(r.ok, false, "备份不可解析应失败");
assert.equal(existsSync(rB4), true, "不可解析备份保留");

rmSync(tmp, { recursive: true, force: true });
console.log("SMOKE TEST ALL PASS");