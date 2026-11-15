/**
 * 前端待确认项一致性断言脚本（不需要任何 npm 依赖）
 *
 * 【怎么跑】node evals/frontend_pending_check.mjs
 *
 * 【在查什么】
 *   页面曾经出现「已选 2 人，却仍提示没说几个人」的错位：
 *   数量读 quality（新数据）、提示文字读 confirmPending（clarify 阶段的一次性旧载荷）。
 *   修法是把三处展示统一到 computed resultPending（单一数据源），并在 final 到达时
 *   清空 clarify 残留。本脚本用 node 的 vm 模块把 frontend/app.js 装进沙箱加载，
 *   拿到 createApp 的 options 后手工拼一个「组件实例」，直接验证这几条不变量。
 *
 * 【为什么这么拼】
 *   app.js 是 Vue 3 的 options 对象，没有构建步骤、也没有导出语句；
 *   沙箱里把 Vue.createApp 换成桩函数即可截获 options。
 *
 * 【断言清单】
 *   1 混合 owner 时 pendingCount 只数用户项
 *   2 pendingHint 等于 resultPending[0].message
 *   3 supplement 模式弹窗清单长度等于 pendingCount
 *   4 final 到达后清空 confirmPending，旧文案不再出现
 *   5 owner=system 的 budget_unverified 不计入待确认
 *   6 retry_lodging 拆成「修改参考地点」与「重新定位」两种动作
 *   7 openingSources 按 poi_ids / facts.poi_id 关联来源，日期不符则不显示
 *   8 issueDetailLines 容忍各种 detail 形状并翻译枚举
 */
import fs from "node:fs";
import vm from "node:vm";

const APP_PATH = new URL("../frontend/app.js", import.meta.url);
const SAMPLES_PATH = new URL("../frontend/samples.js", import.meta.url);
const src = fs.readFileSync(APP_PATH, "utf8");
// app.js 的 data() 里引用了 samples.js 定义的 DEMO_CHIPS / SAMPLE_NOTE，
// 页面里靠先后 <script> 加载共享全局作用域；沙箱里按同样顺序加载即可（同一 context 共享顶层 const）。
const samplesSrc = fs.readFileSync(SAMPLES_PATH, "utf8");

/** 极简 DOMParser 桩：decoratePlanHtml 只用到 querySelectorAll / body.innerHTML */
class StubDomParser {
  parseFromString(html) {
    return {
      body: { innerHTML: String(html || "") },
      querySelectorAll: () => [],
      createElement: () => ({ className: "", appendChild() {}, replaceWith() {} }),
    };
  }
}

let captured = null;
const sandbox = {
  Vue: {
    createApp: (options) => {
      captured = options;
      return { mount: () => options };
    },
  },
  document: { addEventListener() {}, removeEventListener() {} },
  window: { addEventListener() {}, removeEventListener() {} },
  setTimeout: () => 0,
  clearTimeout: () => {},
  console,
  marked: { parse: (text) => String(text || "") },
  DOMPurify: { sanitize: (html) => String(html || "") },
  DOMParser: StubDomParser,
  fetch: () => {
    throw new Error("断言脚本不应发起网络请求");
  },
  AbortController: class {
    constructor() {
      this.signal = {};
    }
    abort() {}
  },
  TextDecoderStream: class {},
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
// 先 samples.js 再 app.js，与 index.html 的 <script> 顺序一致
vm.runInContext(samplesSrc, sandbox, { filename: "samples.js" });
vm.runInContext(src, sandbox, { filename: "app.js" });

if (!captured) {
  console.error("✘ 没能从 app.js 截获 createApp 的 options");
  process.exit(1);
}

const options = captured;
const computedDefs = options.computed || {};
const methodDefs = options.methods || {};

/** 造一个「组件实例」：data 的普通对象 + computed getter + methods 绑定 this */
function makeComponent() {
  const ctx = { ...(options.data ? options.data() : {}) };
  ctx.$refs = {};
  ctx.$nextTick = (cb) => {
    if (typeof cb === "function") cb();
  };
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
// 混合 owner 时，待确认数量只算 owner === "user" 的项（system 项不该让用户觉得要填东西）。
check("1. quality.issues 混合 owner 时 pendingCount 只数用户项", () => {
  const ctx = makeComponent();
  ctx.finalReady = true;
  ctx.quality = {
    status: "partial",
    pending: [],
    issues: [
      { code: "party_unknown", message: "没说几个人，预算只能按人均口径先估算", owner: "user", kind: "pending", action: "confirm_party" },
      { code: "return_choice", message: "返程车次需要你选一趟", owner: "user", kind: "pending", action: "select_return", options: [{ train_no: "G1234", depart: "18:10", arrive: "18:45" }] },
      { code: "budget_unverified", message: "费用未核实", owner: "system", kind: "info", action: null },
      { code: "lodging_area_pending", message: "住宿片区未定", owner: "system", kind: "warning", action: "confirm_lodging" },
    ],
  };
  const expect = 2;
  assert(ctx.pendingCount === expect, `pendingCount 期望 ${expect}，实际 ${ctx.pendingCount}`);
  assert(ctx.resultPending.length === expect, `resultPending 长度期望 ${expect}，实际 ${ctx.resultPending.length}`);
  assert(
    ctx.resultPending.every((item) => item.code !== "budget_unverified" && item.code !== "lodging_area_pending"),
    "system 项不应进入 resultPending"
  );
  return `pendingCount=${ctx.pendingCount}，codes=${ctx.resultPending.map((i) => i.code).join("/")}`;
});

// ---------------------------------------------------------------- 断言 2
// 提示文字必须和数量同源：pendingHint 就是 resultPending 首条的 message。
check("2. pendingHint 等于 resultPending[0].message", () => {
  const ctx = makeComponent();
  ctx.finalReady = true;
  ctx.quality = {
    pending: [],
    issues: [
      { code: "party_unknown", message: "没说几个人，预算只能按人均口径先估算", owner: "user", kind: "pending", action: "confirm_party" },
      { code: "budget_unverified", message: "费用未核实", owner: "system", kind: "info", action: null },
    ],
  };
  const first = ctx.resultPending[0];
  assert(Boolean(first), "resultPending 不应为空");
  assert(ctx.pendingHint === first.message, `pendingHint 期望「${first.message}」，实际「${ctx.pendingHint}」`);
  return `pendingHint=「${ctx.pendingHint}」`;
});

// ---------------------------------------------------------------- 断言 3
// 结果页补信息（supplement）时，弹窗清单必须与数量同源。
check("3. supplement 模式弹窗清单长度等于 pendingCount", () => {
  const ctx = makeComponent();
  ctx.finalReady = true;
  ctx.quality = {
    pending: [],
    issues: [
      { code: "party_unknown", message: "没说几个人", owner: "user", kind: "pending", action: "confirm_party" },
      { code: "budget_scope_unknown", message: "预算总额还是人均没说明", owner: "user", kind: "pending", action: "confirm_budget_scope" },
      { code: "budget_unverified", message: "费用未核实", owner: "system", kind: "info", action: null },
    ],
  };
  ctx.confirmSource = "supplement";
  ctx.confirmPending = []; // 模拟 final 之后已清空的残留
  assert(ctx.confirmList.length === ctx.pendingCount, `confirmList=${ctx.confirmList.length}，pendingCount=${ctx.pendingCount}`);
  assert(ctx.confirmList.length === 2, `confirmList 期望 2，实际 ${ctx.confirmList.length}`);

  // clarify 阶段仍用后端当场给的 pending，不被 quality 覆盖
  ctx.confirmSource = "clarify";
  ctx.confirmPending = [{ field: "lodging", message: "住哪一片？" }];
  assert(ctx.confirmList.length === 1 && ctx.confirmList[0].field === "lodging", "clarify 模式应使用 confirmPending");
  return `supplement=${ctx.pendingCount}，clarify 模式仍读 confirmPending`;
});

// ---------------------------------------------------------------- 断言 4
// 核心回归：clarify 写过 confirmPending 之后来了 final，旧文案不许再出现在页面上。
check("4. final 到达后清空 confirmPending，旧文案不再出现", () => {
  const ctx = makeComponent();
  ctx.finalReady = false;
  ctx.quality = null;
  ctx.handleEvent({
    type: "clarify",
    missing: ["party_size"],
    pending: [{ field: "party_size", message: "没说几个人" }],
    slots: { origin: "新乡", destination: "郑州", date: "2026-11-07" },
  });
  assert(ctx.confirmPending.length === 1, `clarify 后 confirmPending 期望 1，实际 ${ctx.confirmPending.length}`);
  // clarify 阶段弹窗读的是后端当场给的 pending（pendingHint 只用于结果页，此时还没结果）
  assert(ctx.confirmList.length === 1 && ctx.confirmList[0].field === "party_size", "clarify 阶段弹窗应显示后端的 party_size 项");

  // 用户已经在页面里选了 2 人，这一轮 final 的 quality 不再有 party 待确认
  ctx.handleEvent({
    type: "final",
    content: "## 需求摘要\n2 人，新乡到郑州一日游。",
    plan_id: "plan-test",
    quality: {
      status: "partial",
      pending: [],
      issues: [{ code: "budget_unverified", message: "费用未核实", owner: "system", kind: "info", action: null }],
    },
  });
  assert(ctx.finalReady === true, "final 后 finalReady 应为 true");
  assert(ctx.confirmPending.length === 0, `final 后 confirmPending 期望 0，实际 ${ctx.confirmPending.length}`);
  assert(ctx.confirmSource === "clarify", `final 后 confirmSource 应复位为 clarify，实际 ${ctx.confirmSource}`);
  assert(ctx.awaitingClarify === false, "final 后 awaitingClarify 应为 false");
  assert(ctx.confirmOpen === false, "final 后 confirmOpen 应为 false");
  assert(ctx.pendingHint.indexOf("没说几个人") === -1, `final 后 pendingHint 仍含旧文案：「${ctx.pendingHint}」`);
  assert(ctx.pendingCount === 0, `final 后 pendingCount 期望 0，实际 ${ctx.pendingCount}`);
  return `confirmPending 已清空，pendingHint=「${ctx.pendingHint}」`;
});

// ---------------------------------------------------------------- 断言 5
// 后端把「费用未核实」改成系统待核实（owner=system, kind=info, action=null）后，
// 不能再冒充「你需要补充」。
check("5. owner=system 的 budget_unverified 不计入待确认", () => {
  const ctx = makeComponent();
  ctx.finalReady = true;
  ctx.quality = {
    pending: [],
    issues: [{ code: "budget_unverified", message: "费用未核实，预算只是估算", owner: "system", kind: "info", action: null }],
  };
  assert(ctx.pendingCount === 0, `pendingCount 期望 0，实际 ${ctx.pendingCount}`);
  assert(ctx.pendingHint === "", `pendingHint 期望空，实际「${ctx.pendingHint}」`);
  const groups = ctx.issueGroups;
  assert(groups.need.length === 0, `「需要你补充」期望 0 项，实际 ${groups.need.length}`);
  assert(groups.unfinished.length === 1, `「其余待核实」期望 1 项，实际 ${groups.unfinished.length}`);
  return "pendingCount=0，分组落到 unfinished";
});

// ---------------------------------------------------------------- 断言 6
// 住宿重新定位要有两个不同动作：
//   retry_lodging_edit 只改参考地点文本（走确认弹窗 mode="confirm"）
//   retry_lodging_now  真正触发重新定位（走修订原话 mode="revise"）
// 后端若只给旧的 retry_lodging，两者都要渲染出来，不能再只有一个点了没反应的按钮。
check("6. retry_lodging 拆成「修改参考地点」与「重新定位」两种动作", () => {
  const ctx = makeComponent();
  ctx.finalReady = true;

  const legacy = ctx.issueActions({ action: "retry_lodging", options: ["花园路"] });
  assert(legacy.length === 2, `旧 retry_lodging 应渲染 2 个按钮，实际 ${legacy.length}`);
  assert(legacy[0].key === "retry_lodging_edit" && legacy[0].label === "修改参考地点", `第 1 个按钮应为「修改参考地点」，实际 ${legacy[0].key}/${legacy[0].label}`);
  assert(legacy[1].key === "retry_lodging_now" && legacy[1].label === "重新定位", `第 2 个按钮应为「重新定位」，实际 ${legacy[1].key}/${legacy[1].label}`);

  const onlyEdit = ctx.issueActions({ action: "retry_lodging_edit" });
  assert(onlyEdit.length === 1 && onlyEdit[0].key === "retry_lodging_edit", "只给 retry_lodging_edit 时不应多渲染按钮");
  const onlyNow = ctx.issueActions({ action: "retry_lodging_now" });
  assert(onlyNow.length === 1 && onlyNow[0].text === "重新定位住宿区域", `retry_lodging_now 的修订原话应为「重新定位住宿区域」，实际「${onlyNow[0].text}」`);

  // 分派：edit 走弹窗并预填，now 走修订通道
  let presetSeen = null;
  ctx.lastSlots = { origin: "新乡", destination: "郑州", date: "2026-11-07", days: 1 };
  ctx.openConfirmFromResult = (preset) => {
    presetSeen = preset || null;
  };
  ctx.runIssueAction({ action: "retry_lodging", options: ["花园路"] }, legacy[0]);
  assert(presetSeen && presetSeen.lodging === "花园路", `edit 应把选项预填进弹窗，实际 ${JSON.stringify(presetSeen)}`);

  let revised = "";
  ctx.lastPlanId = "plan-test";
  ctx.startRevise = () => {
    revised = ctx.reviseQuery;
  };
  presetSeen = null;
  ctx.runIssueAction({ action: "retry_lodging", options: ["花园路"] }, legacy[1]);
  assert(revised === "重新定位住宿区域", `now 应提交修订原话「重新定位住宿区域」，实际「${revised}」`);
  assert(presetSeen === null, "now 不应再打开确认弹窗");
  return `legacy=2 个按钮，now 修订原话「${revised}」，edit 预填「${"花园路"}」`;
});

// ---------------------------------------------------------------- 断言 7
// 「查看开放依据」必须按 poi_id 关联来源，不能靠 ref 撞运气；日期不适用时不能冒充依据。
check("7. openingSources 按 poi_ids / facts.poi_id 关联来源，日期不符则不显示", () => {
  const ctx = makeComponent();
  ctx.quality = {
    sources: [
      {
        ref: "amap-B001",
        url: "https://example.com/b001",
        label: "景点官网",
        queried_at: "2026-11-06 10:00",
        poi_ids: ["B001"],
        facts: [{ poi_id: "B001", quote: "9:00-17:00，周一闭馆", kind: "opening_hours", applies_on: "2026-11-07" }],
      },
      {
        // 旧快照格式：只有 ref 与 records
        ref: "B002",
        url: "https://example.com/b002",
        label: "来源B",
        records: [{ id: "B002", quote: "冬季 8:30 开门" }],
      },
      { ref: "B999", url: "https://example.com/other", label: "无关来源", records: [{ id: "B999" }] },
    ],
  };

  const hit = ctx.openingSources({ code: "opening_unverified", action: "view_opening", poi_id: "B001", date: "2026-11-07" });
  assert(hit.length === 1, `poi_ids 命中期望 1 条，实际 ${hit.length}`);
  assert(hit[0].quote === "9:00-17:00，周一闭馆", `quote 应取 fact.quote，实际「${hit[0].quote}」`);
  assert(hit[0].kind === "开放时间", `kind 应翻成「开放时间」，实际「${hit[0].kind}」`);
  assert(hit[0].applies_on === "2026-11-07", `应带适用日期，实际「${hit[0].applies_on}」`);

  const wrongDate = ctx.openingSources({ code: "opening_unverified", action: "view_opening", poi_id: "B001", date: "2026-11-09" });
  assert(wrongDate.length === 0, `日期不符时不应给出依据，实际 ${wrongDate.length} 条`);

  const legacyHit = ctx.openingSources({ code: "opening_unverified", action: "view_opening", poi_id: "B002" });
  assert(legacyHit.length === 1 && legacyHit[0].quote === "冬季 8:30 开门", `旧格式 ref/records 应兜底命中，实际 ${JSON.stringify(legacyHit)}`);

  assert(ctx.openingSources({ code: "opening_unverified" }).length === 0, "既无 ref 也无 poi_id 时应返回空");
  return "poi_ids 命中 1 条、日期不符 0 条、旧格式兜底 1 条";
});

// ---------------------------------------------------------------- 断言 8
// detail 是后端新加的可选字段，形状很多；渲染前必须翻译成字符串行，且任何形状都不能抛异常。
check("8. issueDetailLines 容忍各种 detail 形状并翻译枚举", () => {
  const ctx = makeComponent();

  const objLines = ctx.issueDetailLines({
    code: "travel_leg_unknown",
    detail: { reason_text: "超出预算", state: "failed", mode: "driving", depart: "08:30" },
  });
  for (const expect of ["超出预算", "查询失败", "驾车", "出发时刻 08:30"]) {
    assert(objLines.includes(expect), `对象 detail 应包含「${expect}」，实际 ${JSON.stringify(objLines)}`);
  }

  assert(JSON.stringify(ctx.issueDetailLines({ detail: "纯文本" })) === JSON.stringify(["纯文本"]), "字符串 detail 应原样成一行");

  // Lead 明确要求：空对象/detail 缺失都必须安全返回空数组
  assert(JSON.stringify(ctx.issueDetailLines({})) === "[]", `issueDetailLines({}) 期望 []，实际 ${JSON.stringify(ctx.issueDetailLines({}))}`);
  assert(ctx.issueDetailLines({ detail: null }).length === 0, "detail=null 应返回空数组");
  assert(ctx.issueDetailLines({ detail: undefined }).length === 0, "detail=undefined 应返回空数组");
  assert(ctx.issueDetailLines(null).length === 0, "item=null 不应抛异常");
  assert(ctx.issueDetailLines({ detail: [null, "一行", { state: "ok" }] }).length === 2, "数组 detail 应逐项翻译并跳过空值");

  // 结构化 leg 兜底：detail 缺失时仍能说出交通方式
  const legLines = ctx.issueDetailLines({ code: "travel_leg_unknown", leg: { mode: "walking" } });
  assert(legLines.includes("步行"), `leg.mode=walking 应翻成「步行」，实际 ${JSON.stringify(legLines)}`);
  return `对象 detail 4 行、字符串 1 行、空对象 0 行、leg 兜底「步行」`;
});

// ---------------------------------------------------------------- 汇总
check("9. 普通核实项折叠且保留关键风险", () => {
  const ctx = makeComponent();
  ctx.quality = { issues: [
    { code: "travel_leg_unknown", owner: "system", date: "2026-11-09" },
    { code: "travel_leg_unknown", owner: "system", date: "2026-11-08" },
    { code: "opening_missing", owner: "system" },
    { code: "ticket_empty", owner: "system" },
    { code: "rail_window_unknown", owner: "system" },
    { code: "schedule_conflict", owner: "system" },
    { code: "return_basis_pending", owner: "user" },
  ] };
  assert(ctx.verificationGroups.length === 3, "普通明细应汇总为三组");
  assert(ctx.verificationGroups[0].items.length === 2, "交通计数不能丢失明细");
  assert(ctx.verificationGroups[0].items[0].date === "2026-11-08", "明细应按日期排序");
  assert(ctx.visibleSystemIssues.length === 2, "抵离窗口与冲突必须直接显示");
  assert(ctx.issueGroups.need.length === 1, "用户确认项不可折叠到系统详情");
  return "三类汇总、日期排序、关键风险和确认项均保留";
});

// ---------------------------------------------------------------- 断言 10
check("10. 每日密度诊断只在技术记录中生成可读摘要", () => {
  const ctx = makeComponent();
  ctx.quality = { schedule_density: [
    { day: 3, label: "第3天", density: { visit_minutes: 240, target_min: 240, target_max: 360, unknown_legs: 1 } },
  ] };
  assert(ctx.scheduleDiagnostics.length === 1, "应生成一条每日密度摘要");
  assert(ctx.scheduleDiagnostics[0].includes("第3天：0 个主要活动，约 4 小时游览"), `摘要缺少游览时长：${ctx.scheduleDiagnostics[0]}`);
  assert(ctx.scheduleDiagnostics[0].includes("交通耗时待核实"), `摘要缺少未知交通提示：${ctx.scheduleDiagnostics[0]}`);
  return ctx.scheduleDiagnostics[0];
});

// ---------------------------------------------------------------- 断言 11
check("11. 抵离日和未知窗口不显示完整日目标", () => {
  const ctx = makeComponent();
  ctx.quality = { schedule_density: [
    { day: 1, label: "第1天", density: { status: "not_applicable" } },
    { day: 5, label: "第5天", density: { status: "unknown", visit_minutes: 120 } },
  ] };
  assert(ctx.scheduleDiagnostics[0].includes("按抵离窗口安排"), "抵离日不应显示完整日目标");
  assert(ctx.scheduleDiagnostics[1].includes("暂不评估完整日密度"), "未知窗口不应显示完整日目标");
  return ctx.scheduleDiagnostics.join("；");
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
