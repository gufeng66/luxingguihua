/**
 * 前端外部链接与地图沙箱断言脚本（不需要任何 npm 依赖）
 *
 * 【怎么跑】node evals/frontend_url_check.mjs
 *
 * 【在查什么】
 *   后端已经用 safe_evidence_url 只放行官方来源，但前端还有两道自己的口子：
 *     1 模板里 <a :href="src.url"> 直接输出后端给的字符串。一旦某条 URL 绕过
 *       后端校验（或后续有人新增数据源忘了过白名单），javascript: 链接
 *       点一下就是在用户页面里执行脚本。修法是所有外链出口走 safeHref()。
 *     2 地图 iframe 曾同时给 allow-scripts 和 allow-same-origin。这两项一起给，
 *       沙箱内的脚本可以用 document.cookie 读到同源 Cookie，也能摸父页面 DOM，
 *       等于沙箱形同虚设。地图 HTML 自身不需要同源（只有内联脚本、没有
 *       localStorage/fetch/cookie），去掉 allow-same-origin 即可。
 *
 * 【为什么还要读 index.html】
 *   safeHref 写对了但模板里不用它，等于没修。这里静态扫描模板的每一个 :href，
 *   逐个确认走的是 safeHref，避免以后新增链接时漏掉。
 *
 * 【断言清单】
 *   1 safeHref 放行 http / https / mailto
 *   2 safeHref 拦掉 javascript: / data: / vbscript: / file:
 *   3 safeHref 拦掉空值与纯空白
 *   4 safeHref 对大写 scheme 同样生效（JaVaScRiPt:）
 *   5 safeHref 对前导空白的 javascript: 生效
 *   6 index.html 里所有外链 :href 都经过 safeHref
 *   7 index.html 里不再出现 allow-same-origin
 *   8 地图 iframe 仍保留 allow-scripts 与 allow-popups
 */
import fs from "node:fs";
import vm from "node:vm";

const APP_PATH = new URL("../frontend/app.js", import.meta.url);
const SAMPLES_PATH = new URL("../frontend/samples.js", import.meta.url);
const INDEX_PATH = new URL("../frontend/index.html", import.meta.url);

const src = fs.readFileSync(APP_PATH, "utf8");
const samplesSrc = fs.readFileSync(SAMPLES_PATH, "utf8");
const indexHtml = fs.readFileSync(INDEX_PATH, "utf8");

class StubDomParser {
  parseFromString(html) {
    return {
      body: { innerHTML: String(html || "") },
      querySelectorAll: () => [],
      createElement: () => ({ className: "", appendChild() {}, replaceWith() {} }),
    };
  }
}

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
  fetch: () => {
    throw new Error("断言脚本不应发起网络请求");
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
const methodDefs = options.methods || {};
if (typeof methodDefs.safeHref !== "function") {
  console.error("✘ app.js 没有 safeHref 方法，外链缺少统一出口");
  process.exit(1);
}
const ctx = { ...(options.data ? options.data() : {}) };
ctx.window = sandbox.window;
for (const [name, fn] of Object.entries(methodDefs)) {
  if (typeof fn === "function") ctx[name] = fn.bind(ctx);
}
const safeHref = (raw) => ctx.safeHref(raw);

const results = [];
function check(name, fn) {
  try {
    const detail = fn();
    results.push({ ok: true, name, detail: detail == null ? "" : String(detail) });
  } catch (err) {
    results.push({ ok: false, name, detail: err && err.message ? err.message : String(err) });
  }
}
function assert(cond, message) {
  if (!cond) throw new Error(message);
}

// ---------------------------------------------------------------- 断言 1
check("1. safeHref 放行 http / https / mailto", () => {
  const cases = [
    "https://www.dpm.org.cn/visit.html",
    "http://www.chnmuseum.cn/a?b=1#top",
    "mailto:trip@example.com",
  ];
  for (const raw of cases) {
    assert(safeHref(raw) === raw, `应放行 ${raw}，实得 ${JSON.stringify(safeHref(raw))}`);
  }
  return `${cases.length} 条合法链接保留`;
});

// ---------------------------------------------------------------- 断言 2
check("2. safeHref 拦掉 javascript: / data: / vbscript: / file:", () => {
  const cases = [
    "javascript:alert(1)",
    "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
    "vbscript:msgbox(1)",
    "file:///C:/Windows/win.ini",
  ];
  for (const raw of cases) {
    assert(safeHref(raw) === "", `${raw} 必须被拦掉，实得 ${JSON.stringify(safeHref(raw))}`);
  }
  return `${cases.length} 条危险链接被清空`;
});

// ---------------------------------------------------------------- 断言 3
check("3. safeHref 拦掉空值与纯空白", () => {
  for (const raw of ["", "   ", "\t\n", null, undefined]) {
    assert(safeHref(raw) === "", `${JSON.stringify(raw)} 应返回空串`);
  }
  return "空值安全";
});

// ---------------------------------------------------------------- 断言 4
check("4. safeHref 对大写 scheme 同样生效", () => {
  assert(safeHref("JaVaScRiPt:alert(1)") === "", "大小写混写的 javascript: 必须拦掉");
  assert(safeHref("HTTPS://WWW.DPM.ORG.CN/a") !== "", "大写 https 应放行");
  return "大小写不敏感";
});

// ---------------------------------------------------------------- 断言 5
check("5. safeHref 对前导空白的 javascript: 生效", () => {
  assert(safeHref("  javascript:alert(1)") === "", "前导空白不应绕过 scheme 判定");
  return "前导空白安全";
});

// ---------------------------------------------------------------- 断言 6
check("6. index.html 里所有外链 :href 都经过 safeHref", () => {
  const hrefs = [...indexHtml.matchAll(/:href="([^"]+)"/g)].map((m) => m[1].trim());
  assert(hrefs.length > 0, "index.html 里没有找到 :href，模板可能被改坏");
  const offenders = hrefs.filter((expr) => !expr.startsWith("safeHref("));
  assert(
    offenders.length === 0,
    `这些 :href 没有走 safeHref：${offenders.join(" | ")}`
  );
  return `${hrefs.length} 个外链出口统一走 safeHref`;
});

// ---------------------------------------------------------------- 断言 7
check("7. index.html 里不再出现 allow-same-origin", () => {
  assert(
    !/sandbox="[^"]*allow-same-origin/.test(indexHtml),
    "地图 iframe 仍带 allow-same-origin：与 allow-scripts 同时存在会让沙箱内脚本拿到同源权限"
  );
  return "已去掉 allow-same-origin";
});

// ---------------------------------------------------------------- 断言 8
check("8. 地图 iframe 仍保留 allow-scripts 与 allow-popups", () => {
  const sandboxes = [...indexHtml.matchAll(/sandbox="([^"]+)"/g)].map((m) => m[1]);
  assert(sandboxes.length >= 2, `预期 2 个地图 iframe，实际 ${sandboxes.length} 个`);
  for (const value of sandboxes) {
    assert(value.includes("allow-scripts"), `沙箱缺 allow-scripts：${value}`);
    assert(value.includes("allow-popups"), `沙箱缺 allow-popups：${value}`);
  }
  return `${sandboxes.length} 个 iframe 沙箱保留必要权限`;
});

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
