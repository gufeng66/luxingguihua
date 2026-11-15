/**
 * 前端「旧请求污染新请求」竞态断言脚本（不需要任何 npm 依赖）
 *
 * 【怎么跑】node evals/frontend_race_check.mjs
 *
 * 【在查什么】
 *   页面上点「开始规划」之后又立刻改意见重新提交，前端会 abort 旧请求。
 *   但 abort 不是瞬时的：旧请求在 abort 生效前可能已经读到若干帧 SSE，
 *   这些帧照样会走到 consumeSse / handleEvent。修复前实测有两个洞：
 *     1 endRun(controller) 没有身份校验，旧请求的 finally 会把新请求的
 *       loading 关掉，界面在真跑的时候显示「已结束」。
 *     2 handleEvent 不认请求身份，旧请求的 final / error / warning 会直接
 *       写进当前正文与告警列表。
 *   修法是给每次请求发一个递增 runId，消费层与收尾都先比 activeRunId。
 *
 * 【为什么这么拼】
 *   与 frontend_pending_check.mjs 一样，用 node 的 vm 把 app.js 装进沙箱，
 *   把 Vue.createApp 换成桩函数截获 options，再手工拼一个组件实例。
 *
 * 【断言清单】
 *   1 旧 runId 的 step 事件不改进度条
 *   2 旧 runId 的 final 不覆盖正文、不改 lastPlanId
 *   3 旧 runId 的 error 不显示为新请求的失败
 *   4 旧 runId 的 warning 不进入告警列表
 *   5 旧请求的 endRun 不清掉新请求的 loading
 *   6 当前请求的 endRun 正常收尾并清空 activeRunId
 *   7 abort 之后旧请求的 consumeSse 循环立即退出（不再消费后续帧）
 *   8 status 事件写进 runStatus 而不是正文
 *   9 error 事件到达后 loading 立即变 false
 */
import fs from "node:fs";
import vm from "node:vm";

const APP_PATH = new URL("../frontend/app.js", import.meta.url);
const SAMPLES_PATH = new URL("../frontend/samples.js", import.meta.url);
const src = fs.readFileSync(APP_PATH, "utf8");
const samplesSrc = fs.readFileSync(SAMPLES_PATH, "utf8");

class StubDomParser {
  parseFromString(html) {
    return {
      body: { innerHTML: String(html || "") },
      querySelectorAll: () => [],
      createElement: () => ({ className: "", appendChild() {}, replaceWith() {} }),
    };
  }
}

/** 可编程的 SSE body：按脚本吐出若干 text chunk，然后结束 */
function makeSseBody(chunks) {
  let i = 0;
  return {
    pipeThrough() {
      return {
        getReader() {
          return {
            async read() {
              if (i >= chunks.length) return { value: undefined, done: true };
              const value = chunks[i];
              i += 1;
              return { value, done: false };
            },
            async cancel() {
              i = chunks.length;
            },
          };
        },
      };
    },
  };
}

const fetchCalls = [];
let sseScript = [];
const sandbox = {
  Vue: {
    createApp: (options) => {
      sandbox.__captured = options;
      return { mount: () => options };
    },
  },
  document: { addEventListener() {}, removeEventListener() {} },
  window: {
    addEventListener() {},
    removeEventListener() {},
    location: { origin: "http://127.0.0.1:8000", href: "http://127.0.0.1:8000/" },
  },
  setTimeout: () => 0,
  clearTimeout: () => {},
  console,
  marked: { parse: (text) => String(text || "") },
  DOMPurify: { sanitize: (html) => String(html || "") },
  DOMParser: StubDomParser,
  URL,
  fetch: async (url) => {
    fetchCalls.push(String(url));
    return { ok: true, status: 200, text: async () => "", body: makeSseBody(sseScript) };
  },
  AbortController: class {
    constructor() {
      this.signal = { aborted: false };
    }
    abort() {
      this.signal.aborted = true;
    }
  },
  TextDecoderStream: class {},
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(samplesSrc, sandbox, { filename: "samples.js" });
vm.runInContext(src, sandbox, { filename: "app.js" });

const options = sandbox.__captured;
if (!options) {
  console.error("✘ 没能从 app.js 截获 createApp 的 options");
  process.exit(1);
}
const computedDefs = options.computed || {};
const methodDefs = options.methods || {};

function makeComponent() {
  const ctx = { ...(options.data ? options.data() : {}) };
  ctx.$refs = {};
  ctx.$nextTick = (cb) => {
    if (typeof cb === "function") cb();
  };
  ctx.loadMapMeta = () => {};
  ctx.scheduleRender = () => {};
  for (const [name, fn] of Object.entries(methodDefs)) {
    if (typeof fn === "function") ctx[name] = fn.bind(ctx);
  }
  for (const [name, fn] of Object.entries(computedDefs)) {
    if (typeof fn !== "function") continue;
    Object.defineProperty(ctx, name, {
      configurable: true,
      get: () => fn.call(ctx),
    });
  }
  return ctx;
}

function frame(event) {
  return `data: ${JSON.stringify(event)}\n\n`;
}

const results = [];
const pending = [];
function check(name, fn) {
  // 断言按登记顺序串行执行（每个都排进同一条微任务队列），
  // 这样像「中途把 activeRunId 换掉」这类时序断言才能稳定复现。
  pending.push(
    Promise.resolve()
      .then(() => fn())
      .then(
        (detail) => results.push({ ok: true, name, detail: detail == null ? "" : String(detail) }),
        (err) => results.push({ ok: false, name, detail: err && err.message ? err.message : String(err) })
      )
  );
}
function assert(cond, message) {
  if (!cond) throw new Error(message);
}

/** 造一个「新请求正在进行」的组件状态：activeRunId = 2 */
function makeRunningComponent() {
  const ctx = makeComponent();
  ctx.loading = true;
  ctx.finalReady = false;
  ctx.errorText = "";
  ctx.finalText = "";
  ctx.warnings = [];
  ctx.activeRunId = 2;
  return ctx;
}

// ---------------------------------------------------------------- 断言 1
check("1. 旧 runId 的 step 事件不改进度条", () => {
  const ctx = makeRunningComponent();
  const before = JSON.stringify(ctx.steps);
  ctx.handleEvent({ type: "step", id: "summary", status: "active" }, 1);
  assert(JSON.stringify(ctx.steps) === before, "旧请求的 step 不应改进度条");
  ctx.handleEvent({ type: "step", id: "summary", status: "active" }, 2);
  const step = ctx.steps.find((s) => s.id === "summary");
  assert(step && step.status === "active", "当前请求的 step 应生效");
  return "旧请求被丢弃，当前请求生效";
});

// ---------------------------------------------------------------- 断言 2
check("2. 旧 runId 的 final 不覆盖正文、不改 lastPlanId", () => {
  const ctx = makeRunningComponent();
  ctx.handleEvent({ type: "final", content: "旧方案正文", plan_id: "old-id" }, 1);
  assert(ctx.finalText === "", "旧请求的 final 不应写入正文");
  assert(!ctx.finalReady, "旧请求的 final 不应置 finalReady");
  assert(ctx.lastPlanId !== "old-id", "旧请求的 final 不应改 lastPlanId");
  ctx.handleEvent({ type: "final", content: "新方案正文", plan_id: "new-id" }, 2);
  assert(ctx.finalText === "新方案正文", "当前请求的 final 应写正文");
  assert(ctx.lastPlanId === "new-id", "当前请求的 final 应写 lastPlanId");
  return "旧 final 丢弃，新 final 生效";
});

// ---------------------------------------------------------------- 断言 3
check("3. 旧 runId 的 error 不显示为新请求的失败", () => {
  const ctx = makeRunningComponent();
  ctx.handleEvent({ type: "error", message: "旧请求挂了" }, 1);
  assert(!ctx.errorText, "旧请求的 error 不应写进 errorText");
  assert(ctx.loading, "旧请求的 error 不应关掉 loading");
  ctx.handleEvent({ type: "error", message: "新请求挂了" }, 2);
  assert(ctx.errorText === "新请求挂了", "当前请求的 error 应显示");
  return "旧 error 丢弃，新 error 生效";
});

// ---------------------------------------------------------------- 断言 4
check("4. 旧 runId 的 warning 不进入告警列表", () => {
  const ctx = makeRunningComponent();
  ctx.handleEvent({ type: "warning", code: "old_warn", message: "旧告警" }, 1);
  assert(ctx.warnings.length === 0, "旧请求的 warning 不应进列表");
  ctx.handleEvent({ type: "warning", code: "new_warn", message: "新告警" }, 2);
  assert(ctx.warnings.length === 1, "当前请求的 warning 应进列表");
  return `告警 ${ctx.warnings.length} 条`;
});

// ---------------------------------------------------------------- 断言 5
check("5. 旧请求的 endRun 不清掉新请求的 loading", () => {
  const ctx = makeRunningComponent();
  const oldController = { signal: { aborted: true } };
  ctx.endRun(oldController, 1);
  assert(ctx.loading === true, "旧请求收尾不应关掉新请求的 loading");
  assert(ctx.closedEarly === false, "旧请求收尾不应把新请求判成提前断开");
  assert(ctx.activeRunId === 2, "旧请求收尾不应清空当前请求身份");
  return "新请求仍在运行";
});

// ---------------------------------------------------------------- 断言 6
check("6. 当前请求的 endRun 正常收尾并清空 activeRunId", () => {
  const ctx = makeRunningComponent();
  const controller = { signal: { aborted: false } };
  ctx.endRun(controller, 2);
  assert(ctx.loading === false, "当前请求收尾应关掉 loading");
  assert(ctx.activeRunId === null, "当前请求收尾应清空身份");
  return "loading=false, activeRunId=null";
});

// ---------------------------------------------------------------- 断言 7
check("7. abort 之后旧请求的 consumeSse 循环立即退出", async () => {
  // 制造「请求刚发出、还没读到第一帧就被新请求接管」的时序：
  // consumeSse 的第一个 await 之后把 activeRunId 换掉，循环顶部的身份比对
  // 应当立刻 cancel reader 并 break，旧帧一帧都不进 handleEvent。
  const ctx = makeComponent();
  ctx.accessKey = "";
  ctx.activeRunId = 2;
  sseScript = [frame({ type: "token", content: "旧内容1" }), frame({ type: "token", content: "旧内容2" })];
  const consumed = [];
  ctx.handleEvent = (event, runId) => consumed.push({ runId, content: event.content });
  const running = ctx.consumeSse({ query: "x" }, { aborted: false }, 2);
  ctx.activeRunId = 3; // 新请求接管
  await running;
  assert(consumed.length === 0, `旧请求不应再消费帧，实际消费 ${consumed.length} 帧`);
  return "旧流已停止消费";
});

// ---------------------------------------------------------------- 断言 8
check("8. status 事件写进 runStatus 而不是正文", () => {
  const ctx = makeRunningComponent();
  ctx.handleEvent({ type: "status", message: "正在理解需求" }, 2);
  assert(ctx.runStatus === "正在理解需求", "status 应写 runStatus");
  assert(ctx.finalText === "", "status 不应写正文");
  return ctx.runStatus;
});

// ---------------------------------------------------------------- 断言 9
check("9. error 事件到达后 loading 立即变 false", () => {
  const ctx = makeRunningComponent();
  ctx.handleEvent({ type: "error", message: "服务端出错" }, 2);
  assert(ctx.loading === false, "error 是终态，应立即结束 loading");
  assert(ctx.errorText === "服务端出错", "error 文本应保留");
  assert(ctx.runStatus === "", "失败后应清掉过程提示");
  return "loading=false";
});

await Promise.all(pending);
let failed = 0;
for (const item of results) {
  if (item.ok) {
    console.log(`✔ ${item.name} —— ${item.detail}`);
  } else {
    failed += 1;
    console.error(`✘ ${item.name} —— ${item.detail}`);
  }
}
if (failed) {
  console.error(`\n共 ${failed} 条断言失败（${results.length} 条中）。`);
  process.exitCode = 1;
} else {
  console.log(`\n全部 ${results.length} 条断言通过。`);
}
