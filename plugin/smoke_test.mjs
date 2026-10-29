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
 *  6. 宿主非 2xx（400/500）→ 明确失败，auth.json 字节不变、不产生备份
 *
 * 环境：TOKENHUB_DISABLE_SPAWN=1（不 spawn 假 exe）；OPENCODE_AUTH_PATH=<临时文件>。
 */
import { strict as assert } from "node:assert";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { createServer } from "node:http";
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

// 8. resolveOpencodeAuthPath(override)：显式配置 → 显式环境路径 → 自动发现
const resolveAuth = hooks._tokenhubResolveAuthPath;
assert.equal(typeof resolveAuth, "function", "hooks 应暴露 _tokenhubResolveAuthPath");
const savedAuthEnv = process.env.OPENCODE_AUTH_PATH;
const savedXdg = process.env.XDG_DATA_HOME;
const xdgDir = join(tmp, "xdg");
const defaultAuth = join(xdgDir, "opencode", "auth.json");
mkdirSync(dirname(defaultAuth), { recursive: true });
const defaultBytes = JSON.stringify(
  { "opencode-go": { type: "api", key: "sk-default" }, other: { type: "api", key: "x" } },
  null,
  2,
);
writeFileSync(defaultAuth, defaultBytes);

function backupsOf(target) {
  const prefix = target.split(/[\\/]/).pop() + ".tokenhub.bak-";
  return readdirSync(dirname(target)).filter((n) => n.startsWith(prefix));
}

async function startHub(extra) {
  const h = await TokenHub({
    serverUrl: new URL("http://127.0.0.1:1/"), // 不可达 → 走文件分支
    directory: tmp,
    ...extra,
  });
  const c = h._tokenhubControl;
  return {
    hooks: h,
    switch: async (key) => {
      const res = await fetch(`http://127.0.0.1:${c.port}/switch`, {
        method: "POST",
        headers: { "X-TokenHub-Token": c.token, "content-type": "application/json" },
        body: JSON.stringify({ key }),
      });
      return { status: res.status, body: await res.json() };
    },
  };
}

function startStatusStub(status) {
  const server = createServer((req, res) => {
    req.on("end", () => {
      res.writeHead(status, { "content-type": "application/json" });
      res.end(JSON.stringify({ ok: false, error: "host says no" }));
    });
    req.resume();
  });
  return new Promise((resolve, reject) => {
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const addr = server.address();
      resolve({
        url: `http://127.0.0.1:${addr.port}/`,
        close: () =>
          new Promise((done) => {
            server.closeAllConnections();
            server.close(() => done());
          }),
      });
    });
  });
}

function startSilentStub(onRequest) {
  const server = createServer(() => {
    onRequest();
    // 故意不响应：模拟宿主 API 已接收请求但超时
  });
  return new Promise((resolve, reject) => {
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const addr = server.address();
      resolve({
        url: `http://127.0.0.1:${addr.port}/`,
        close: () =>
          new Promise((done) => {
            server.closeAllConnections();
            server.close(() => done());
          }),
      });
    });
  });
}

// 8a. override 不存在但默认文件存在：只会在 override 新建，默认文件字节不变
delete process.env.OPENCODE_AUTH_PATH;
process.env.XDG_DATA_HOME = xdgDir;
assert.equal(resolveAuth(), defaultAuth, "无 override/env 时应自动发现默认文件");
const overrideMissing = join(tmp, "override-missing", "auth.json");
assert.equal(resolveAuth(overrideMissing), overrideMissing, "override 缺失时仍应唯一指向 override");
const hub8a = await startHub({ authPath: overrideMissing });
const r8a = await hub8a.switch("sk-override-created");
assert.equal(r8a.status, 200);
assert.equal(r8a.body.ok, true);
assert.equal(r8a.body.method, "file");
assert.equal(
  JSON.parse(readFileSync(overrideMissing, "utf8"))["opencode-go"].key,
  "sk-override-created",
  "override 不存在时应按明确目标新建",
);
assert.equal(readFileSync(defaultAuth, "utf8"), defaultBytes, "显式 override 写入不得改动默认文件");
await hub8a.hooks.dispose();

// 8b. override 损坏但默认文件有效：明确报错，两个文件字节都不变
const overrideCorrupt = join(tmp, "override-corrupt", "auth.json");
mkdirSync(dirname(overrideCorrupt), { recursive: true });
const corruptBytes = "{broken json";
writeFileSync(overrideCorrupt, corruptBytes);
const hub8b = await startHub({ authPath: overrideCorrupt });
const r8b = await hub8b.switch("sk-override-corrupt");
assert.equal(r8b.status, 200);
assert.equal(r8b.body.ok, false, "损坏的显式目标不得静默改用默认文件");
assert.ok(String(r8b.body.error).includes("读取 auth.json 失败"), "应明确报告显式目标读取失败");
assert.equal(readFileSync(overrideCorrupt, "utf8"), corruptBytes, "损坏的显式目标不得被覆盖");
assert.equal(readFileSync(defaultAuth, "utf8"), defaultBytes, "显式目标损坏时默认文件不变");
await hub8b.hooks.dispose();

// 8c. 环境显式路径优先于自动发现；显式配置又优先于环境路径
const envMissing = join(tmp, "env-explicit", "auth.json");
process.env.OPENCODE_AUTH_PATH = envMissing;
assert.equal(resolveAuth(), envMissing, "环境显式路径即使不存在也不得回退默认文件");
assert.equal(resolveAuth(overrideMissing), overrideMissing, "显式配置应优先于环境路径");
const hub8c = await startHub({});
const r8c = await hub8c.switch("sk-env-created");
assert.equal(r8c.body.ok, true);
assert.equal(r8c.body.method, "file");
assert.equal(
  JSON.parse(readFileSync(envMissing, "utf8"))["opencode-go"].key,
  "sk-env-created",
  "环境显式路径不存在时应按该目标新建",
);
assert.equal(readFileSync(defaultAuth, "utf8"), defaultBytes, "环境显式路径写入不得改动默认文件");
await hub8c.hooks.dispose();
process.env.OPENCODE_AUTH_PATH = savedAuthEnv;

// 9. timeout 且宿主已写入目标 key → 核对文件确认成功，不再次写文件
const timeoutWrittenAuth = join(tmp, "timeout-written", "auth.json");
mkdirSync(dirname(timeoutWrittenAuth), { recursive: true });
writeFileSync(
  timeoutWrittenAuth,
  JSON.stringify({ "opencode-go": { type: "api", key: "sk-timeout-old" }, other: 1 }),
);
const stubBytes = JSON.stringify({
  "opencode-go": { type: "api", key: "sk-timeout-written" },
  stub: "written",
});
const stubWritten = await startSilentStub(() => writeFileSync(timeoutWrittenAuth, stubBytes));
const hub9 = await startHub({ serverUrl: new URL(stubWritten.url), authPath: timeoutWrittenAuth });
const started9 = Date.now();
const r9 = await hub9.switch("sk-timeout-written");
assert.ok(Date.now() - started9 >= 2500, "应实际观察到宿主 API 超时");
assert.equal(r9.body.ok, true, "文件已含目标 key 时超时应升级为成功");
assert.equal(r9.body.method, "api");
assert.equal(r9.body.verified, "auth-file");
assert.equal(readFileSync(timeoutWrittenAuth, "utf8"), stubBytes, "超时确认后不得再次写文件");
assert.deepEqual(backupsOf(timeoutWrittenAuth), [], "超时确认后不得产生备份/直写副作用");
await hub9.hooks.dispose();
await stubWritten.close();

// 10. timeout 且宿主未写入 → uncertain，保留原文件且不盲目直写
const timeoutPendingAuth = join(tmp, "timeout-pending", "auth.json");
mkdirSync(dirname(timeoutPendingAuth), { recursive: true });
const pendingBytes = JSON.stringify({ "opencode-go": { type: "api", key: "sk-timeout-old" } });
writeFileSync(timeoutPendingAuth, pendingBytes);
const stubPending = await startSilentStub(() => {});
const hub10 = await startHub({ serverUrl: new URL(stubPending.url), authPath: timeoutPendingAuth });
const started10 = Date.now();
const r10 = await hub10.switch("sk-timeout-pending");
assert.ok(Date.now() - started10 >= 2500, "应实际观察到宿主 API 超时");
assert.equal(r10.body.ok, false, "无法确认超时结果时不得报成功");
assert.equal(r10.body.uncertain, true);
assert.equal(r10.body.status, "timeout_uncertain");
assert.ok(String(r10.body.error).includes("未再次写入"), "应明确说明未再次写文件");
assert.equal(readFileSync(timeoutPendingAuth, "utf8"), pendingBytes, "超时未确认时不得直写文件");
assert.deepEqual(backupsOf(timeoutPendingAuth), [], "超时未确认时不得产生备份");
await hub10.hooks.dispose();
await stubPending.close();

// 11. 宿主非 2xx（400/500）拒绝 → 明确失败，绝不直写 auth.json
const rejectedAuth = join(tmp, "host-rejected", "auth.json");
mkdirSync(dirname(rejectedAuth), { recursive: true });
const rejectedBytes = JSON.stringify(
  {
    "opencode-go": { type: "api", key: "sk-keep-current" },
    other: { type: "api", key: "x" },
  },
  null,
  2,
);
writeFileSync(rejectedAuth, rejectedBytes);
for (const code of [400, 500]) {
  const stubRejected = await startStatusStub(code);
  const hub11 = await startHub({
    serverUrl: new URL(stubRejected.url),
    authPath: rejectedAuth,
  });
  const r11 = await hub11.switch(`sk-rejected-${code}`);
  assert.equal(r11.status, 200, `宿主 ${code} 时控制接口仍应返回 200（业务失败）`);
  assert.equal(r11.body.ok, false, `宿主 ${code} 拒绝不得报成功`);
  assert.equal(r11.body.status, "host_rejected", `宿主 ${code} 应标记 host_rejected`);
  assert.ok(
    String(r11.body.error).includes(`HTTP ${code}`),
    `错误应包含宿主状态码 ${code}`,
  );
  assert.ok(String(r11.body.error).includes("未写入"), "错误应明确说明未写认证文件");
  assert.notEqual(r11.body.method, "file", "宿主拒绝不得声称认证文件已更新");
  assert.equal(
    readFileSync(rejectedAuth, "utf8"),
    rejectedBytes,
    `宿主 ${code} 拒绝后 auth.json 字节不变`,
  );
  assert.deepEqual(
    backupsOf(rejectedAuth),
    [],
    `宿主 ${code} 拒绝后不得产生备份`,
  );
  await hub11.hooks.dispose();
  await stubRejected.close();
}

// 12. 文件降级：目录创建/备份失败 → 明确失败，不建立目标、占位文件不变
const blockedParent = join(tmp, "parent-is-file");
const blockedBytes = "not a directory";
writeFileSync(blockedParent, blockedBytes);
const blockedAuth = join(blockedParent, "auth.json");
const hub12 = await startHub({ authPath: blockedAuth });
const r12 = await hub12.switch("sk-backup-fail");
assert.equal(r12.body.ok, false, "备份/目录创建失败不得报成功");
assert.ok(
  String(r12.body.error).includes("备份 auth.json 失败"),
  "应明确报告备份/目录创建失败",
);
assert.equal(readFileSync(blockedParent, "utf8"), blockedBytes, "占位文件不得被改动");
assert.equal(existsSync(blockedAuth), false, "失败后不应出现目标 auth.json");
await hub12.hooks.dispose();

// 13. 文件降级：原子替换失败 → 报告原账号未受影响，auth.json 字节不变
const roAuth = join(tmp, "readonly-auth", "auth.json");
mkdirSync(dirname(roAuth), { recursive: true });
const roBytes = JSON.stringify({
  "opencode-go": { type: "api", key: "sk-readonly-old" },
});
writeFileSync(roAuth, roBytes);
chmodSync(roAuth, 0o444); // Windows：置只读属性，使 rename 覆盖目标失败
const hub13 = await startHub({ authPath: roAuth });
const r13 = await hub13.switch("sk-readonly-new");
assert.equal(r13.body.ok, false, "替换失败不得报成功");
assert.equal(r13.body.method, "file");
assert.ok(
  String(r13.body.error).includes("原账号未受影响"),
  "应明确说明原账号未受影响",
);
assert.equal(readFileSync(roAuth, "utf8"), roBytes, "替换失败后 auth.json 字节不变");
assert.deepEqual(
  readdirSync(dirname(roAuth)).filter((n) => n.includes(".tokenhub.tmp-")),
  [],
  "替换失败后不应残留临时文件",
);
await hub13.hooks.dispose();
chmodSync(roAuth, 0o666); // 恢复可写，保证临时目录可清理

if (savedXdg === undefined) delete process.env.XDG_DATA_HOME;
else process.env.XDG_DATA_HOME = savedXdg;

rmSync(tmp, { recursive: true, force: true });
console.log("SMOKE TEST ALL PASS");