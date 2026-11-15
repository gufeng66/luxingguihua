/**
 * 旅游规划多智能体 — 前端逻辑（Vue3 CDN，无构建步骤）
 *
 * 【小白怎么理解？】
 *   模板在 index.html；本文件管状态、点按钮、读后端 SSE。
 *   规划很慢：服务器用 Server-Sent Events 一帧帧推 JSON（data: {...}）。
 *   密钥只在后端 .env，浏览器里没有 API Key。
 *
 * 事件类型（与 planner.pipeline 一致）：
 *   step / status / slots / clarify / token / final / error / warning / subagent / tool
 *
 * 高德链接工具在 map.js，须先于本文件加载。
 * 演示稿在 samples.js，须先于本文件加载。
 */

const { createApp } = Vue;

/** 六个阶段的 id 必须和后端 step 事件一致。查车票与找景点可以同时进行。 */
const STEP_DEFS = [
  { id: "understand", label: "理解需求" },
  { id: "ticket", label: "查车票" },
  { id: "map", label: "找景点" },
  { id: "facts", label: "核对信息" },
  { id: "schedule", label: "排行程" },
  { id: "summary", label: "写成行程" },
];

const CRITICAL_KEYS = ["origin", "destination", "date"];

function nameList(text) {
  return String(text || "")
    .split(/[,，、]/)
    .map((part) => part.trim())
    .filter(Boolean);
}
const SLOT_ORDER = [
  "origin",
  "destination",
  "date",
  "lodging",
  "days",
  "budget",
  "preferences",
  "pace",
  "must_visit",
  "avoid",
  "transport_mode",
];
const SLOT_LABELS = {
  origin: "出发地",
  destination: "目的地",
  date: "出行日期",
  days: "游玩天数",
  budget: "预算",
  preferences: "偏好",
  pace: "出行节奏",
  lodging: "住宿地",
  must_visit: "必去",
  avoid: "不去",
  transport_mode: "市内交通",
};


function decoratePlanHtml(html) {
  /** 行程表中的「第N天」列表项加节点；表格单独横向滚动。 */
  const doc = new DOMParser().parseFromString(html, "text/html");
  const dayRe = /^第\s*[0-9０-９一二三四五六七八九十]+\s*天/;
  doc.querySelectorAll("table").forEach((table) => {
    const wrap = doc.createElement("div");
    wrap.className = "table-scroll";
    table.replaceWith(wrap);
    wrap.appendChild(table);
  });
  doc.querySelectorAll("h2").forEach((heading) => {
    if (heading.textContent.trim() !== "行程表") return;
    let node = heading.nextElementSibling;
    while (node && node.tagName !== "H2") {
      if (node.matches("ul, ol")) {
        node.querySelectorAll(":scope > li").forEach((item) => {
          if (dayRe.test(item.textContent.trim())) item.classList.add("is-day");
        });
      }
      node = node.nextElementSibling;
    }
  });
  return doc.body.innerHTML;
}

function emptySteps() {
  /** 每次开跑重置四步：pending → active / done / skipped */
  return STEP_DEFS.map((s) => ({
    id: s.id,
    label: s.label,
    status: "pending",
    tools: [],
    reason: null,
    elapsedMs: null,
  }));
}

/**
 * 明细项 detail / leg 里的枚举值 → 中文。
 * 只用于展示，不参与任何请求、分组或状态判断。
 */
const ISSUE_STATE_LABELS = {
  ok: "已查询",
  failed: "查询失败",
  timeout: "查询超时",
  not_queried: "未查询",
  skipped: "未查询",
  unknown: "状态未知",
};
const ISSUE_MODE_LABELS = {
  walking: "步行",
  driving: "驾车",
  public_transit: "公共交通",
  transit: "公共交通",
  transfer: "换乘",
  taxi: "打车",
  riding: "骑行",
};
/** 开放依据的 kind → 中文（认不出的原样显示，不丢信息） */
const OPENING_KIND_LABELS = {
  opening_hours: "开放时间",
  last_entry: "最晚入场",
  latest_entry: "最晚入场",
  closed: "闭馆",
  closure: "闭馆",
  closed_days: "闭馆日",
  reservation: "预约要求",
};

createApp({
  data() {
    return {
      query: "",
      accessKey: "",
      loading: false,
      steps: emptySteps(),
      finalText: "",
      renderedHtml: "",
      errorText: "",
      /** 后端 status 事件的过程提示文本（如「正在理解需求」），只做过程展示 */
      runStatus: "",
      warnings: [],
      finalReady: false,
      closedEarly: false,
      quality: null,
      savedUrl: "",
      mapUrl: "",
      traceUrl: "",
      timings: null,
      lastPlanId: "",
      lastSlots: null,
      /** 当前方案有效的口径字段（不在 TravelSlots 里，只能从 final 带回来） */
      lastRequestCtx: null,
      /** 草稿归属：记录这份未提交表单属于哪个 plan_id，用于区分「新方案首次打开」和「同方案继续编辑」 */
      confirmDraftPlanId: "",
      /** 当前进行中的请求上下文：统一记录 query、父方案 ID 与来源，供澄清提交沿用，避免拿错父方案或残留上次修改意见。 */
      reqCtx: null,
      lastBaseQuery: "",
      reviseQuery: "",
      abortController: null,
      /** 递增的请求序号：用于判定「这一帧事件属于哪一次请求」。旧请求即使在
       * abort 之前已经收到数据，也不能再改新请求的正文、告警、错误与地图链接。 */
      runSeq: 0,
      /** 当前有效请求的序号，null 表示本轮没有进行中的请求 */
      activeRunId: null,
      awaitingClarify: false,
      /** 澄清弹窗被用户主动关掉：不再拦住整页操作，但要留一个「继续补充」的入口把草稿找回来 */
      clarifyDeferred: false,
      /** 统一「补充与确认」弹窗：clarify 事件和结果页的待确认项都走它 */
      confirmOpen: false,
      confirmSource: "clarify",
      confirmPending: [],
      /** 「查看开放依据」当前展开的明细项 key（空串表示都收起），纯前端展开、不发请求 */
      openingIssueKey: "",
      clarifyForm: {
        origin: "",
        destination: "",
        date: "",
        days: 1,
        budget: "",
        preferences: "",
        pace: "",
        lodging: "",
        must_visit: "",
        avoid: "",
        transport_mode: "public_transit",
        party_size: "",
        budget_scope: "",
        lodging_scope: "",
        intercity_mode: "",
        return_required: "",
      },
      _confirmBaseline: {},
      missingFields: [],
      defaultsApplied: [],
      _renderTimer: null,
      _lastRenderAt: 0,
      demoChips: DEMO_CHIPS,
      chipHint: "",
      sampleNote: "",
      copyLabel: "复制行程",
      resultTab: "plan",
      sidebarOpen: true,
      resultUserScrolled: false,
      amapPreviewUrl: "",
      amapAppUrl: "",
      mapFullscreen: false,
      _mapHtml: "",
      _mapTasks: [],
      mapDayFilter: "all",
    };
  },

  computed: {
    canSubmit() {
      const len = this.query.trim().length;
      return !this.loading && !this.awaitingClarify && len > 0 && len <= 300;
    },
    canConfirmClarify() {
      if (this.loading) return false;
      const keys = CRITICAL_KEYS.slice();
      if ((this.missingFields || []).indexOf("lodging") !== -1) keys.push("lodging");
      return keys.every((k) => String(this.clarifyForm[k] || "").trim());
    },
    /**
     * 待确认项的单一数据源：只放「真正需要用户决定」的项。
     * 来源一：quality.pending（这些就是用户确认项）；
     * 来源二：quality.issues 里 owner === "user" 的项。
     * 旧数据没有 owner 字段时，回退成「kind === "pending" 且带 action」才算用户项。
     * 注意：这里不能读 confirmPending —— 那是 clarify 事件的一次性载荷，方案更新后必然过期。
     */
    resultPending() {
      if (!this.finalReady) return [];
      const quality = this.quality || {};
      const items = [];
      const seen = new Set();
      const push = (field, source) => {
        const key = String(field || "");
        if (!key || seen.has(key)) return;
        seen.add(key);
        const detail = source.detail == null ? "" : String(source.detail);
        items.push({
          field: key,
          actions: [],
          message: String(source.message || ""),
          code: String(source.code || key),
          action: source.action || null,
          options: Array.isArray(source.options) ? source.options : [],
          detail,
        });
      };
      for (const item of quality.pending || []) {
        if (!item) continue;
        push(item.action || item.code, item);
      }
      for (const issue of quality.issues || []) {
        if (!issue) continue;
        const isUser = issue.owner ? issue.owner === "user" : issue.kind === "pending" && Boolean(issue.action);
        if (!isUser) continue;
        push(issue.action || issue.code, issue);
      }
      return items;
    },
    /** 兼容旧调用：待确认项统一走 resultPending，不再回读可能过期的 confirmPending。 */
    confirmItems() {
      return this.resultPending;
    },
    /** 结果页出现待补项时，引导用户打开同一个确认弹窗。 */
    pendingCount() {
      if (!this.finalReady) return 0;
      return this.resultPending.length;
    },
    /**
     * 确认弹窗里要列出的清单：clarify 阶段用后端当场给的 pending，
     * supplement（结果页补信息）阶段与 pendingCount / pendingHint 同源。
     */
    confirmList() {
      if (this.confirmSource === "clarify") return this.confirmPending || [];
      return this.resultPending;
    },
    queryLen() {
      return this.query.length;
    },
    phase() {
      if (this.loading) return "running";
      if (this.awaitingClarify) return "clarify";
      if (this.errorText) return "failed";
      if (this.closedEarly) return "incomplete";
      if (this.sampleNote && !this.finalReady) return "sample";
      if (this.finalReady) return "done";
      return "idle";
    },
    statusLabel() {
      const base = {
        idle: "待开始",
        running: "生成中",
        clarify: "需要补充",
        done: "已完成",
        failed: "失败",
        incomplete: "未完成",
        sample: "示例预览",
      }[this.phase];
      if (this.phase !== "done") return base;
      // 「已完成」只表示流程走完；能不能照着走由质量状态决定。
      if (this.pendingCount) return "待你确认";
      const status = (this.quality || {}).status;
      if (status === "partial") return "已生成，部分未核实";
      if (status === "degraded") return "已生成，有降级";
      return base;
    },
    statusBadgeClass() {
      const base = {
        idle: "is-idle",
        running: "is-busy",
        clarify: "is-wait",
        done: "is-done",
        failed: "is-fail",
        incomplete: "is-incomplete",
        sample: "is-sample",
      }[this.phase];
      if (this.phase !== "done") return base;
      if (this.pendingCount) return "is-wait";
      const status = (this.quality || {}).status;
      if (status === "partial") return "is-warn";
      if (status === "degraded") return "is-fail";
      return base;
    },
    nowTitle() {
      return {
        running: "正在生成行程",
        clarify: "还差几项才能继续",
        done: "行程已经写好",
        failed: "这次没有完成",
        incomplete: "方案尚未完成",
        sample: "这是示例，不是查询结果",
      }[this.phase] || "";
    },
    draftNote() {
      if (!this.finalText || this.sampleNote || this.finalReady) return "";
      if (this.phase === "running") return "正在写入，还不是最终稿";
      if (this.phase === "incomplete") return "连接已结束，方案尚未完成";
      return "";
    },
    nowDetail() {
      if (this.phase === "running") {
        const active = this.steps.filter((s) => s.status === "active");
        if (active.length >= 2) {
          return "正在" + active.map((s) => s.label).join("，同时") + "。页面上的文字还不是最终稿。";
        }
        if (active.length === 1) {
          return "当前在「" + active[0].label + "」。页面上的文字还不是最终稿。";
        }
        return "页面上的文字还不是最终稿。";
      }
      if (this.phase === "clarify") return "先补下面标出的项。已经识别的内容可以直接改。";
      if (this.phase === "done") return "可以复制或下载。想改路线，用行程下面的修改。";
      if (this.phase === "failed") return this.errorText;
      if (this.phase === "incomplete") return "连接已结束，方案尚未完成";
      if (this.phase === "sample") return this.sampleNote;
      return "";
    },
    stepGroups() {
      const byId = Object.fromEntries(this.steps.map((s) => [s.id, s]));
      const groups = [];
      if (byId.understand) groups.push({ id: "understand", pair: false, steps: [byId.understand] });
      const pair = ["ticket", "map"].map((id) => byId[id]).filter(Boolean);
      if (pair.length) groups.push({ id: "pair", pair: pair.length > 1, steps: pair });
      for (const id of ["facts", "schedule", "summary"]) {
        if (byId[id]) groups.push({ id, pair: false, steps: [byId[id]] });
      }
      const used = new Set(["understand", "ticket", "map", "facts", "schedule", "summary"]);
      const rest = this.steps.filter((s) => !used.has(s.id));
      if (rest.length) groups.push({ id: "rest", pair: false, steps: rest });
      return groups;
    },
    clarifySections() {
      const missing = new Set(this.missingFields || []);
      const extraKeys = ["preferences", "pace", "must_visit", "avoid", "transport_mode"];
      const need = [];
      const known = [];
      const extra = [];
      for (const key of SLOT_ORDER) {
        if (missing.has(key)) need.push(key);
        else if (extraKeys.includes(key)) extra.push(key);
        else known.push(key);
      }
      return { need, known, extra };
    },
    qualityHeadline() {
      if (!this.quality) return "";
      const pending = this.pendingCount;
      if (this.quality.status === "degraded") return "这次有降级，未核实的内容不能当成事实";
      if (this.quality.status === "partial") {
        return pending ? "方案已生成，但还有几项要你确认" : "有些信息还没核实，出行前请再确认";
      }
      return "核对过的信息比较齐";
    },
    /**
     * 结果页分三组：需要你补充（owner=user）、系统待核实 / 其他说明。
     * owner 由后端给出（user / system），kind 只表示提示级别、仅供样式着色，不再参与分组。
     * 旧快照没有 owner 字段时，回退成「带 action 才算用户项」。
     */
    issueGroups() {
      const issues = (this.quality && this.quality.issues) || [];
      const isUser = (item) => (item && item.owner ? item.owner === "user" : Boolean(item && item.action));
      return {
        need: issues.filter(isUser),
        unfinished: issues.filter((item) => item && !isUser(item)),
        notes: (this.quality && this.quality.notes) || [],
      };
    },
    // 普通明细折叠；抵离窗口、冲突及其他风险仍直接显示。
    verificationGroups() {
      const definitions = [
        ["travel_leg_unknown", "交通耗时待核实", "段"],
        ["opening_missing", "开放规则待核实", "项"],
        ["ticket_empty", "未找到车次的查询", "次"],
      ];
      return definitions.map(([code, label, unit]) => ({
        code, label, unit,
        items: this.issueGroups.unfinished.filter((item) => item.code === code)
          .slice().sort((a, b) => String(a.date || "").localeCompare(String(b.date || ""))),
      })).filter((group) => group.items.length);
    },
    visibleSystemIssues() {
      const folded = new Set(this.verificationGroups.map((group) => group.code));
      return this.issueGroups.unfinished.filter((item) => !folded.has(item.code));
    },
    /** 技术记录里显示原始失败原因，不把内部细节摊在正文。 */
    techReasons() {
      const quality = this.quality || {};
      const rows = [];
      if (quality.route_error) rows.push(`路线查询 ${quality.route_error}`);
      if (quality.search_error) rows.push(`开放规则查询 ${quality.search_error}`);
      const applicable = quality.ticket_applicability;
      if (applicable && applicable.reasons && applicable.reasons.length) {
        rows.push(`车次适用性 ${applicable.reasons.join("、")}`);
      }
      return rows;
    },
    scheduleDiagnostics() {
      return ((this.quality && this.quality.schedule_density) || []).map((row) => {
        const density = row.density || {};
        const label = row.label || `第${row.day || "?"}天`;
        if (density.status === "not_applicable") return `${label}：按抵离窗口安排，不按完整游览日评估。`;
        if (density.status === "unknown") return `${label}：抵离窗口未知，暂不评估完整日密度。`;
        const visit = Number(density.visit_minutes);
        const targetMin = Number(density.target_min);
        const targetMax = Number(density.target_max);
        if (!Number.isFinite(visit) || !Number.isFinite(targetMin) || !Number.isFinite(targetMax)) {
          return `${label}：游览时长待估，暂不显示完整日目标。`;
        }
        const target = `${targetMin / 60}–${targetMax / 60}`;
        const traffic = Number(density.unknown_legs || 0) ? "；交通耗时待核实" : "";
        const activities = Number(density.activity_count || 0);
        const countMin = Number(density.target_count_min);
        const countMax = Number(density.target_count_max);
        const countText = Number.isFinite(countMin) && Number.isFinite(countMax)
          ? `，景点目标 ${countMin}–${countMax} 个`
          : "";
        const weight = Number(density.activity_weight);
        const weightText = Number.isFinite(weight) ? `，活动权重 ${weight}` : "";
        const estimate = Number(density.estimated_activities || 0) ? "，含规划估算" : "";
        const tight = density.over_target ? "；活动偏紧" : "";
        return `${label}：${activities} 个主要活动，约 ${visit / 60} 小时游览（目标 ${target} 小时）${countText}${weightText}${estimate}${tight}${traffic}`;
      });
    },
    showProgress() {
      return this.phase === "running" || this.phase === "clarify" || this.phase === "failed" || this.phase === "incomplete" || this.phase === "done";
    },
    /** 查询依据：同一次查询只占一行，说明这是 1 次查询、N 条记录。 */
    groupedSources() {
      const rows = (this.quality && this.quality.sources) || [];
      const byKey = new Map();
      for (const item of rows) {
        const key = item.ref || item.url || "unknown";
        let hit = byKey.get(key);
        if (!hit) {
          hit = { ref: item.ref || "", label: "", url: "", queried_at: "", rows: 0, queries: 0, facts: 0 };
          byKey.set(key, hit);
        }
        hit.rows += 1;
        // 新快照一行就是一次查询（带 record_count/fact_count）；
        // 旧快照是一条事实一行、两个字段都没有，此时行数就是记录数。
        hit.queries = Math.max(hit.queries, Number(item.record_count) || 0);
        hit.facts = Math.max(hit.facts, Number(item.fact_count) || 0);
        if (!hit.label && item.label) hit.label = item.label;
        if (!hit.url && item.url) hit.url = item.url;
        if (!hit.queried_at && item.queried_at) hit.queried_at = item.queried_at;
      }
      return Array.from(byKey.values()).map((hit) => ({
        ref: hit.ref,
        label: hit.label,
        url: hit.url,
        queried_at: hit.queried_at,
        record_count: Math.max(hit.queries, hit.rows),
        fact_count: Math.max(hit.facts, hit.rows),
      }));
    },
    sourcesSummary() {
      /** 「1 次查询，20 条记录」比「查询依据（20）」更准确：同一次查询不重复计数。 */
      const rows = this.groupedSources;
      if (!rows.length) return "查询依据";
      const queries = rows.reduce((sum, item) => sum + item.record_count, 0);
      const facts = rows.reduce((sum, item) => sum + item.fact_count, 0);
      return `${queries} 次查询，${facts} 条记录`;
    },
    /** 结果页「下一步」的一句话提示，取第一条待确认项（与数量同源）。 */
    pendingHint() {
      const items = this.resultPending;
      if (!items.length) return "";
      return items[0].message || "";
    },
    mapDays() {
      return listMapDays(this._mapTasks);
    },
    visibleMapTasks() {
      return filterTasksByDay(this._mapTasks, this.mapDayFilter);
    },
    mapViewerHtml() {
      if (!this._mapTasks.length) return "";
      return buildMapViewerHtml(this._mapTasks, this.mapDayFilter);
    },
  },

  watch: {
    renderedHtml() {
      this.$nextTick(() => this.autoScrollResult());
    },
    loading(val) {
      if (!val && this.finalReady && !this.awaitingClarify && !this.errorText) {
        this.sidebarOpen = false;
      }
    },
    awaitingClarify(val) {
      if (!val) return;
      this.sidebarOpen = true;
      this.$nextTick(() => this.focusConfirm());
    },
    confirmOpen(val) {
      /** 原生 <dialog> 没有 v-model：开合都要显式调 showModal/close。 */
      const dialog = this.$refs.confirmDialog;
      if (!dialog) return;
      try {
        if (val && !dialog.open && typeof dialog.showModal === "function") dialog.showModal();
        else if (val && !dialog.open) dialog.setAttribute("open", "");
        else if (!val && dialog.open && typeof dialog.close === "function") dialog.close();
        else if (!val && dialog.open) dialog.removeAttribute("open");
      } catch (_e) {
        /* 老浏览器不支持 showModal 时退回普通显示 */
        if (val) dialog.setAttribute("open", "true");
      }
    },
    resultTab(val) {
      if (val !== "map" && document.fullscreenElement) {
        document.exitFullscreen().catch(() => {});
      }
    },
  },

  methods: {
    applyChip(chip) {
      if (chip.mode === "revise" && this.lastPlanId) {
        this.reviseQuery = chip.query;
        this.chipHint = "已填入修订框。下面仍是当前方案，提交修改才会重跑";
        return;
      }
      if (chip.mode === "revise") {
        this.query = REVISE_STANDALONE;
      } else {
        this.query = chip.query;
      }
      if (chip.plan) {
        this.presentSample(chip.plan);
      } else {
        this.resetPlanState();
      }
      this.chipHint = chip.hint || "";
    },

    presentSample(plan) {
      this.resetPlanState();
      this.finalText = plan;
      this.sampleNote = SAMPLE_NOTE;
      this.resultTab = "plan";
      this.resultUserScrolled = false;
      this.sidebarOpen = true;
      this.scheduleRender(true);
    },

    async copyFinal() {
      const text = this.finalText || "";
      if (!text) return;
      try {
        await navigator.clipboard.writeText(text);
        this.copyLabel = "已复制";
        setTimeout(() => {
          this.copyLabel = "复制行程";
        }, 1500);
      } catch (_e) {
        this.copyLabel = "复制失败";
        setTimeout(() => {
          this.copyLabel = "复制行程";
        }, 1500);
      }
    },

    isNarrowViewport() {
      return window.matchMedia("(max-width: 900px)").matches;
    },

    prepareRunUi() {
      this.resultTab = "plan";
      this.resultUserScrolled = false;
      this.sidebarOpen = !this.isNarrowViewport();
    },

    formatSeconds(ms) {
      if (ms == null || Number.isNaN(Number(ms))) return "—";
      return (Number(ms) / 1000).toFixed(1);
    },

    reasonText(code) {
      return {
        route_no_rail: "这次不需要查火车票",
        route_no_map: "这次不需要地图",
        mcp_unavailable: "车票服务不可用",
        user_no_map: "你说了不要地图",
        user_no_poi: "你说了不要找景点",
        user_no_route: "未查询路线耗时（用户明确不查路线，景点与行程仍保留，但交通耗时未核实）",
        reuse_parent: "沿用上一版结果",
        sse_timeout: "本次规划超时",
        not_dispatched: "这次没有安排查票",
        empty: "没有查到可用结果",
        dispatch_timeout: "这一步超时了",
      }[code] || "记录里保留了原始代码";
    },

    safeHref(raw) {
      /** 外部链接出口：只允许 http/https/mailto。
       * 后端已经过滤过证据链接，这里是第二层防护——前端渲染出的地址
       * 一旦是 javascript:/data:，点一下就是在用户页面里执行脚本。 */
      const text = String(raw || "").trim();
      if (!text) return "";
      let parsed = null;
      try {
        parsed = new URL(text, window.location.origin);
      } catch (_e) {
        return "";
      }
      const protocol = parsed.protocol.toLowerCase();
      if (!["http:", "https:", "mailto:"].includes(protocol)) return "";
      return text;
    },

    stepStateText(status) {
      return { active: "进行中", done: "完成", skipped: "已跳过", pending: "等待" }[status] || status;
    },

    autoScrollResult() {
      if (!this.loading || this.resultUserScrolled) return;
      const el = this.$refs.resultEnd;
      if (el) el.scrollIntoView({ block: "end" });
    },

    onPageScroll() {
      const gap = document.documentElement.scrollHeight - window.scrollY - window.innerHeight;
      this.resultUserScrolled = gap > 120;
    },

    async loadMapMeta() {
      this.amapPreviewUrl = "";
      this.amapAppUrl = "";
      this._mapHtml = "";
      const url = this.mapUrl;
      if (!url) return;
      try {
        const res = await fetch(url);
        if (!res.ok) return;
        const html = await res.text();
        if (this.mapUrl !== url) return;
        this._mapHtml = html;
        const tasks = parseAmapTaskData(html);
        this._mapTasks = assignTaskDays(tasks || []);
        this.mapDayFilter = this.mapDays.length > 1 ? "all" : this.mapDays[0] || "all";
        this.refreshMapLinks();
      } catch (_e) {
        if (this.mapUrl === url) {
          this.amapPreviewUrl = "";
          this.amapAppUrl = "";
          this._mapTasks = [];
        }
      }
    },

    openExternal(url) {
      if (!url) return;
      window.open(url, "_blank", "noopener");
    },

    refreshMapLinks() {
      const tasks = this.visibleMapTasks;
      this.amapPreviewUrl = tasks.length ? generateMapLink(tasks) : "";
      this.amapAppUrl = generateAmapAppLink(firstPoi(tasks));
    },

    setMapDay(day) {
      this.mapDayFilter = day;
      this.refreshMapLinks();
    },

    openAmapPreview() {
      this.openExternal(this.amapPreviewUrl || this.mapUrl);
    },

    openAmapApp() {
      this.openExternal(this.amapAppUrl);
    },

    mapPoiFallback() {
      return firstPoi(parseAmapTaskData(this._mapHtml));
    },

    resolveMapAnchorUrl(anchor) {
      if (!anchor) return "";
      const attr = (anchor.getAttribute("href") || "").trim();
      const abs = (anchor.href || "").trim();
      const id = anchor.id || "";
      const text = (anchor.textContent || "").replace(/\s+/g, "");
      const isOnline = id === "amapOnline" || text.indexOf("高德在线") !== -1;
      const isApp =
        id === "amapApp" ||
        id === "amapUri" ||
        text.indexOf("高德App") !== -1 ||
        text.indexOf("高德APP") !== -1;
      const custom = AMAP_APP_SCHEMES.test(attr) || AMAP_APP_SCHEMES.test(abs);

      if (isApp || custom) {
        if (isHttpUrl(attr) || (isHttpUrl(abs) && !isPlaceholderHref(attr))) {
          return isHttpUrl(attr) ? attr : abs;
        }
        return amapAppLinkFromHref(attr || abs, this.mapPoiFallback()) || this.amapAppUrl;
      }
      if (isOnline || isPlaceholderHref(attr)) {
        if (!isPlaceholderHref(attr) && isHttpUrl(attr)) return attr;
        if (!isPlaceholderHref(attr) && isHttpUrl(abs)) return abs;
        return this.amapPreviewUrl || abs;
      }
      if (isHttpUrl(abs)) return abs;
      return "";
    },

    onMapFrameClick(event) {
      /** iframe 沙箱可能拦弹窗：拦截高德链接改由父页 window.open */
      const anchor = event.target && event.target.closest ? event.target.closest("a") : null;
      if (!anchor) return;
      const target = (anchor.getAttribute("target") || "").toLowerCase();
      const attr = (anchor.getAttribute("href") || "").trim();
      const abs = (anchor.href || "").trim();
      const id = anchor.id || "";
      const text = (anchor.textContent || "").replace(/\s+/g, "");
      const wantsBlank = target === "_blank" || target === "_new";
      const isAmap =
        id === "amapOnline" ||
        id === "amapApp" ||
        id === "amapUri" ||
        text.indexOf("高德") !== -1 ||
        AMAP_APP_SCHEMES.test(attr) ||
        AMAP_APP_SCHEMES.test(abs);
      if (!wantsBlank && !isAmap) return;
      const url = this.resolveMapAnchorUrl(anchor);
      if (!url) return;
      event.preventDefault();
      event.stopPropagation();
      this.openExternal(url);
    },

    onMapFrameLoad() {
      /** 给地图 iframe 内的链接接上父页的 openExternal。
       * 地图 iframe 去掉了 allow-same-origin，它的 contentDocument 现在是跨源不可访问的，
       * 这里会正常地走 catch 返回；iframe 内的 target=_blank 链接由 allow-popups 自己弹出。
       * 保留这段逻辑是为了兼容旧页面或本地调试时放开沙箱的场景。 */
      const iframe = this.$refs.mapFrame;
      if (!iframe) return;
      let win;
      let doc;
      try {
        win = iframe.contentWindow;
        doc = iframe.contentDocument;
      } catch (_e) {
        return;
      }
      if (!win || !doc) return;
      const parentOpen = this.openExternal.bind(this);
      try {
        win.open = function (url) {
          if (url) parentOpen(String(url));
          return null;
        };
      } catch (_e) {
        /* 沙箱仍可能禁止改写 */
      }
      doc.addEventListener("click", this.onMapFrameClick, true);
    },

    toggleMapFullscreen() {
      const el = this.$refs.mapPane;
      if (!el) return;
      if (document.fullscreenElement) {
        document.exitFullscreen().catch(() => {});
        return;
      }
      const req = el.requestFullscreen || el.webkitRequestFullscreen;
      if (req) req.call(el);
    },

    onFullscreenChange() {
      this.mapFullscreen = document.fullscreenElement === this.$refs.mapPane;
    },

    buildReportHtml() {
      const itinerary = DOMPurify.sanitize(marked.parse(this.finalText || ""));
      const preview = this.amapPreviewUrl
        ? `<p class="report-amap"><a href="${escapeHtmlAttr(this.amapPreviewUrl)}" target="_blank" rel="noopener">在高德地图预览整套路线</a></p>`
        : "";
      const inner = `<h1>行程方案</h1>${preview}<div class="markdown-body">${itinerary}</div>`;
      const extraCss =
        "#itinerary-report{padding:16px 18px 20px;border-bottom:1px solid rgba(15,61,56,.16)}" +
        "#itinerary-report h1{font-size:20px;margin:0 0 10px}" +
        "#itinerary-report .markdown-body{line-height:1.7;font-size:14px}" +
        "#itinerary-report table{border-collapse:collapse;width:100%;margin:8px 0}" +
        "#itinerary-report th,#itinerary-report td{border:1px solid #c9d6d3;padding:8px 10px;text-align:left}" +
        "#itinerary-report thead th{background:rgba(15,61,56,.08)}";

      if (this._mapHtml) {
        const doc = new DOMParser().parseFromString(this._mapHtml, "text/html");
        if (!doc.getElementById("itinerary-report-style")) {
          const style = doc.createElement("style");
          style.id = "itinerary-report-style";
          style.textContent = extraCss;
          (doc.head || doc.documentElement).appendChild(style);
        }
        let host = doc.getElementById("itinerary-report");
        if (!host) {
          host = doc.createElement("section");
          host.id = "itinerary-report";
          if (doc.body) doc.body.insertBefore(host, doc.body.firstChild);
          else doc.documentElement.appendChild(host);
        }
        host.innerHTML = inner;
        return "<!DOCTYPE html>\n" + doc.documentElement.outerHTML;
      }

      return (
        "<!DOCTYPE html>\n<html lang=\"zh-CN\"><head><meta charset=\"utf-8\">" +
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">" +
        "<title>行程方案</title><style>" + extraCss + "</style></head><body>" +
        `<section id="itinerary-report">${inner}</section></body></html>`
      );
    },

    async downloadReport() {
      if (!this.finalText) return;
      if (this.mapUrl && !this._mapHtml) await this.loadMapMeta();
      const html = this.buildReportHtml();
      const blob = new Blob([html], { type: "text/html;charset=utf-8" });
      const objectUrl = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = objectUrl;
      a.download = `旅游规划-${this.lastPlanId || "plan"}_report.html`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(objectUrl);
    },

    isCriticalMissing(key) {
      return this.missingFields.includes(key);
    },

    isDefaultApplied(key) {
      return this.defaultsApplied.includes(key);
    },

    slotLabel(key) {
      return SLOT_LABELS[key] || key;
    },

    resetPlanState() {
      this.steps = emptySteps();
      this.finalText = "";
      this.renderedHtml = "";
      this.errorText = "";
      this.warnings = [];
      this.finalReady = false;
      this.closedEarly = false;
      this.quality = null;
      this.savedUrl = "";
      this.mapUrl = "";
      this.traceUrl = "";
      this.timings = null;
      this.reviseQuery = "";
      this.lastPlanId = "";
      this.lastSlots = null;
      this.lastRequestCtx = null;
      this.confirmDraftPlanId = "";
      this.reqCtx = null;
      this.lastBaseQuery = "";
      this.awaitingClarify = false;
      this.clarifyDeferred = false;
      this.confirmOpen = false;
      this.confirmPending = [];
      this.openingIssueKey = "";
      this.missingFields = [];
      this.defaultsApplied = [];
      this.amapPreviewUrl = "";
      this.amapAppUrl = "";
      this._mapHtml = "";
      this._mapTasks = [];
      this.mapDayFilter = "all";
      this.mapFullscreen = false;
      this.sampleNote = "";
    },

    /**
     * 告警去重：同一个 code + ref + kind 只留一条。
     * 后端的 warning 会按阶段重复发（景点、住宿、路线各一次），直接 push 会刷出一屏。
     */
    pushWarning(event) {
      const code = String(event.code || "");
      const ref = String(event.ref || "");
      const kind = String(event.kind || "warning");
      const message = String(event.message || "出现告警");
      const key = `${code}|${ref}|${kind}`;
      const hit = this.warnings.find((item) => item && item.key === key);
      if (hit) {
        hit.reason = event.reason || hit.reason;
        return;
      }
      this.warnings.push({
        key,
        code,
        ref,
        kind,
        message,
        reason: event.reason || "",
        action: event.action || "",
      });
    },

    applyStepEvent(event) {
      const step = this.steps.find((s) => s.id === event.id);
      if (!step) return;
      step.status = event.status;
      if (event.label) step.label = event.label;
      if (event.reason) step.reason = event.reason;
      if (event.elapsed_ms != null) step.elapsedMs = event.elapsed_ms;
    },

    currentActiveStep() {
      return this.steps.find((s) => s.status === "active") || null;
    },

    attachToolToActive(event) {
      const active = this.currentActiveStep();
      if (!active) return;
      active.tools.push({
        name: event.name || "tool",
        args: event.args || {},
        result: null,
      });
    },

    attachToolResult(content) {
      const active = this.currentActiveStep();
      if (!active || !active.tools.length) return;
      active.tools[active.tools.length - 1].result = content;
    },

    scheduleRender(force) {
      /** Markdown 渲染节流约 100ms，避免每个 token 都 parse 一次 */
      const now = Date.now();
      const run = () => {
        this._lastRenderAt = Date.now();
        this._renderTimer = null;
        if (!this.finalText) {
          this.renderedHtml = "";
          return;
        }
        this.renderedHtml = decoratePlanHtml(DOMPurify.sanitize(marked.parse(this.finalText)));
      };
      if (force) {
        if (this._renderTimer) {
          clearTimeout(this._renderTimer);
          this._renderTimer = null;
        }
        run();
        return;
      }
      const elapsed = now - this._lastRenderAt;
      if (elapsed >= 100 && !this._renderTimer) {
        run();
        return;
      }
      if (this._renderTimer) return;
      this._renderTimer = setTimeout(run, Math.max(0, 100 - elapsed));
    },

    handleEvent(event, runId) {
      /** 消化一帧 SSE：改进度条、确认卡、打字机正文或最终结果。
       * runId 不是当前请求时直接丢弃：旧请求在 abort 生效前可能已经读到
       * 数据，若放任它写 finalText/errorText/warnings，新请求的结果会被
       * 旧数据污染（甚至旧请求的报错会显示在新请求上）。 */
      if (runId != null && runId !== this.activeRunId) return;
      const type = event.type;

      if (type === "status") {
        // 后端会先发一句「正在理解需求」这类状态文本。它只是过程提示，
        // 不能覆盖最终正文，也不参与最终结果的保存。
        this.runStatus = String(event.message || "");
        return;
      }

      if (type === "step") {
        this.applyStepEvent(event);
        return;
      }
      if (type === "subagent") {
        const name = event.name || "";
        let id = null;
        if (name.includes("ticket")) id = "ticket";
        else if (name.includes("map")) id = "map";
        else if (name.includes("summary")) id = "summary";
        if (id) {
          const step = this.steps.find((s) => s.id === id);
          if (step && step.status !== "skipped") {
            step.status = "active";
          }
        }
        return;
      }
      if (type === "tool") {
        this.attachToolToActive(event);
        return;
      }
      if (type === "tool_result") {
        this.attachToolResult(event.content || "");
        return;
      }
      if (type === "clarify") {
        this.awaitingClarify = true;
        this.clarifyDeferred = false;
        this.loading = false;
        this.missingFields = event.missing || [];
        this.defaultsApplied = event.defaults_applied || [];
        this.confirmPending = event.pending || [];
        this.confirmSource = "clarify";
        this.fillClarifyForm(event.slots || {}, event.request_context || null);
        this.confirmOpen = true;
        this.$nextTick(() => this.focusConfirm());
        return;
      }
      if (type === "warning") {
        this.pushWarning(event);
        return;
      }
      if (type === "slots") {
        this.lastSlots = event.slots || null;
        return;
      }
      if (type === "token") {
        if (!this.finalReady) {
          this.finalText += event.content || "";
        }
        this.scheduleRender(false);
        return;
      }
      if (type === "final") {
        this.finalReady = true;
        this.finalText = event.content || this.finalText;
        this.savedUrl = event.saved_url || "";
        this.mapUrl = event.map_url || "";
        this.traceUrl = event.trace_url || "";
        this.lastPlanId = event.plan_id || "";
        this.lastRequestCtx = event.request_context || null;
        // 收到新方案：之前那份未提交草稿已过期，重置草稿归属。
        this.confirmDraftPlanId = "";
        // 本轮请求已完成：清掉请求上下文，避免下次操作误沿用。
        this.reqCtx = null;
        // 新方案落地：上一轮 clarify 的待确认清单、弹窗状态全部作废，
        // 待确认项统一由 resultPending（读 quality）重新算，否则会残留旧文案。
        this.confirmPending = [];
        this.confirmSource = "clarify";
        this.awaitingClarify = false;
        this.confirmOpen = false;
        this.clarifyDeferred = false;
        this.openingIssueKey = "";
        this.timings = event.timings || null;
        this.quality = event.quality || null;
        this.closedEarly = false;
        const stay = this.resultUserScrolled;
        this.resultUserScrolled = true;
        this.scheduleRender(true);
        if (!stay) {
          this.$nextTick(() => {
            const el = this.$refs.resultStart;
            if (el) el.scrollIntoView({ block: "start" });
          });
        }
        this.loadMapMeta();
        return;
      }
      if (type === "error") {
        // error 是独立终态：立刻收掉「生成中」，否则流还挂在服务端时
        // 界面会一直显示进行中，用户以为还在跑。正文保留已收到的部分。
        this.closedEarly = false;
        this.errorText = event.message || "规划失败";
        this.runStatus = "";
        this.loading = false;
      }
    },

    async consumeSse(body, signal, runId) {
      /** POST /api/plan，按空行拆 SSE 帧，解析 data: 后的 JSON。
       * runId 是本轮请求的身份，每帧都要核对：旧请求在 abort 生效前收到的
       * 数据同样会走到这里，如果不带身份，旧结果会覆盖新请求的正文。 */
      if (this.accessKey) {
        const login = await fetch("/api/session", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ api_key: this.accessKey }), signal,
        });
        if (!login.ok) throw new Error(`HTTP ${login.status}: ${await login.text()}`);
        this.accessKey = "";
      }
      const response = await fetch("/api/plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal,
      });
      if (!response.ok) {
        const errText = await response.text();
        throw new Error(`HTTP ${response.status}: ${errText}`);
      }
      if (!response.body) {
        throw new Error("当前浏览器不支持 ReadableStream");
      }
      const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        if (runId !== this.activeRunId) {
          // 已被新请求取代：停止消费，避免旧流继续往里灌事件
          try { await reader.cancel(); } catch (_e) { /* 忽略 */ }
          break;
        }
        buffer += value;
        const parts = buffer.split("\n\n");
        buffer = parts.pop() || "";
        for (const part of parts) {
          const trimmed = part.trim();
          if (!trimmed) continue;
          const line = trimmed.split("\n").find((l) => l.startsWith("data:"));
          if (!line) continue;
          const jsonText = line.replace(/^data:\s*/, "").trim();
          try {
            this.handleEvent(JSON.parse(jsonText), runId);
          } catch (_e) {
            /* 跳过坏帧 */
          }
        }
      }
    },

    endRun(controller, runId) {
      /** 结束一轮请求：只认当前那一轮的收尾。
       * 旧请求的 finally 也会走到这里，若不加身份校验，它会把新请求的
       * loading 关掉、甚至判定成「提前断开」。 */
      if (runId != null && runId !== this.activeRunId) return;
      if (!controller.signal.aborted && !this.finalReady && !this.awaitingClarify && !this.errorText) {
        this.closedEarly = true;
      }
      if (!this.awaitingClarify) this.loading = false;
      if (runId != null) this.activeRunId = null;
    },

    onResultTabKey(event) {
      const order = ["plan", "map"];
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      let index = order.indexOf(this.resultTab);
      if (event.key === "ArrowRight") index = (index + 1) % order.length;
      else if (event.key === "ArrowLeft") index = (index - 1 + order.length) % order.length;
      else if (event.key === "Home") index = 0;
      else index = order.length - 1;
      this.resultTab = order[index];
      const el = index === 0 ? this.$refs.tabPlan : this.$refs.tabMap;
      if (el) el.focus();
    },

    initClarifyForm(slots, ctx) {
      /** 完整重建表单：用于「新方案首次打开」，不做任何旧值保留。 */
      const s = slots || {};
      const c = ctx || {};
      this.clarifyForm = {
        origin: String(s.origin || ""),
        destination: String(s.destination || ""),
        date: String(s.date || ""),
        days: s.days != null ? s.days : 1,
        budget: String(s.budget || ""),
        preferences: String(s.preferences || ""),
        pace: String(s.pace || ""),
        lodging: String(s.lodging || ""),
        must_visit: Array.isArray(s.must_visit) ? s.must_visit.join("、") : String(s.must_visit || ""),
        avoid: Array.isArray(s.avoid) ? s.avoid.join("、") : String(s.avoid || ""),
        transport_mode: String(s.transport_mode || "public_transit"),
        // 口径字段不在 TravelSlots 里，从 final.request_context 还原当前方案的有效值。
        party_size: c.party_size != null ? String(c.party_size) : "",
        budget_scope: String(c.budget_scope || ""),
        lodging_scope: String(c.lodging_scope || ""),
        intercity_mode: String(c.intercity_mode || ""),
        return_required: c.return_required == null ? "" : String(Boolean(c.return_required)),
      };
      this._confirmBaseline = {
        party_size: this.clarifyForm.party_size, budget_scope: this.clarifyForm.budget_scope,
        lodging_scope: this.clarifyForm.lodging_scope, intercity_mode: this.clarifyForm.intercity_mode,
        return_required: this.clarifyForm.return_required,
      };
    },

    fillClarifyForm(slots, ctx) {
      /** clarify 事件：后端发来权威槽位，完整重建（不做部分保留）。 */
      this.initClarifyForm(slots, ctx);
      this.confirmDraftPlanId = this.lastPlanId || "";
    },

    focusConfirm() {
      const dialog = this.$refs.confirmDialog;
      if (dialog && typeof dialog.focus === "function") dialog.focus();
    },

    openConfirmFromResult(preset) {
      /**
       * 结果页的「待补充」入口：从当前方案槽位 + 口径完整重建表单。
       * 清单必须与 pendingCount / pendingHint 同源（resultPending），不能回读过期的 confirmPending。
       * preset 为可选项：明细项按钮可传入 { lodging } 预填住宿参考地点。
       */
      if (!this.lastSlots) return;
      this.confirmSource = "supplement";
      this.confirmPending = this.resultPending.slice();
      const planId = this.lastPlanId || "";
      if (this.confirmDraftPlanId !== planId) {
        // 新方案（或上次草稿属于别的方案）：完整重建，不继承残留草稿。
        this.initClarifyForm(this.lastSlots, this.lastRequestCtx);
        this.confirmDraftPlanId = planId;
      }
      // 同一方案已有未提交草稿：保留当前 clarifyForm（含用户清空的字段），不再灌旧槽位。
      const quality = this.quality || {};
      for (const issue of quality.issues || []) {
        if (issue.action === "confirm_lodging" && !this.clarifyForm.lodging_scope) this.clarifyForm.lodging_scope = "";
        if (issue.action === "confirm_budget" && !this.clarifyForm.budget_scope) this.clarifyForm.budget_scope = "";
      }
      // 住宿参考地点：预填到草稿，用户提交后走既有 mode="confirm"（lodging 变化触发后端 map_only）。
      if (preset && preset.lodging) this.clarifyForm.lodging = String(preset.lodging);
      this.confirmOpen = true;
      this.$nextTick(() => this.focusConfirm());
    },

    /**
     * 明细项按钮：按后端给的 action 生成，全部走现有通道（修订原话 / 弹窗 / 纯前端展开）。
     * 不新增请求字段、不新增接口。
     */
    issueActions(item) {
      const action = item && item.action ? String(item.action) : "";
      const rows = [];
      if (action === "select_return") {
        const options = Array.isArray(item.options) ? item.options : [];
        for (const opt of options) {
          if (!opt || typeof opt !== "object") continue;
          const trainNo = String(opt.train_no || "").trim();
          if (!trainNo) continue;
          const depart = String(opt.depart || "").trim();
          const arrive = String(opt.arrive || "").trim();
          const span = depart || arrive ? `（${depart}→${arrive}）` : "";
          rows.push({ key: `return-${trainNo}-${rows.length}`, label: `返程坐 ${trainNo}${span}`, text: `返程坐 ${trainNo}` });
        }
        return rows;
      }
      if (action === "recheck_tickets") {
        // 后端 _TICKET_REFRESH_HINTS 会识别这句原话，走既有 mode="revise"。
        rows.push({ key: "recheck_tickets", label: "重新查询余票", text: "重新查询余票" });
        return rows;
      }
      if (action === "rerun_route") {
        // 项目没有单路段重试接口，文案只说「重新查询路线」（后端 _explicit_map_reshuffle 识别）。
        rows.push({ key: "rerun_route", label: "重新查询路线", text: "重新规划路线" });
        return rows;
      }
      if (action === "view_opening") {
        rows.push({ key: "view_opening", label: "查看开放依据", text: "" });
        return rows;
      }
      if (action === "retry_lodging_edit") {
        // 只改参考地点文本：打开补充弹窗，提交后走既有 mode="confirm"。
        rows.push({ key: "retry_lodging_edit", label: "修改参考地点", text: "" });
        return rows;
      }
      if (action === "retry_lodging_now") {
        // 真正触发重新定位：走既有修订通道（mode="revise"），后端按原话重查。
        rows.push({ key: "retry_lodging_now", label: "重新定位", text: "重新定位住宿区域" });
        return rows;
      }
      if (action === "retry_lodging") {
        // 后端只给旧 action 时按「编辑」解释，并额外补一个真能触发重新定位的按钮。
        rows.push({ key: "retry_lodging_edit", label: "修改参考地点", text: "" });
        rows.push({ key: "retry_lodging_now", label: "重新定位", text: "重新定位住宿区域" });
        return rows;
      }
      return rows;
    },

    /**
     * 明细项的补充说明行：把后端给的 detail（字符串 / 对象 / 数组，均可能缺失）翻成可读中文行。
     * 纯展示，不参与请求与分组；任何形状都不能抛异常。
     */
    issueDetailLines(item) {
      const lines = [];
      const push = (value) => {
        const text = value == null ? "" : String(value).trim();
        if (text && !lines.includes(text)) lines.push(text);
      };
      const fromObject = (obj) => {
        if (!obj || typeof obj !== "object" || Array.isArray(obj)) return;
        if (obj.reason_text != null) push(obj.reason_text);
        else if (obj.reason != null) push(obj.reason);
        const state = obj.state == null ? "" : String(obj.state).trim();
        if (state) push(ISSUE_STATE_LABELS[state] || `状态：${state}`);
        const mode = obj.mode == null ? "" : String(obj.mode).trim();
        if (mode) push(ISSUE_MODE_LABELS[mode] || `交通方式：${mode}`);
        const depart = obj.depart == null ? "" : String(obj.depart).trim();
        if (depart) push(`出发时刻 ${depart}`);
      };
      if (!item || typeof item !== "object") return lines;
      const detail = item.detail;
      if (typeof detail === "string") {
        push(detail);
      } else if (Array.isArray(detail)) {
        for (const one of detail) {
          if (typeof one === "string") push(one);
          else fromObject(one);
        }
      } else if (detail && typeof detail === "object") {
        fromObject(detail);
      }
      // travel_leg_unknown 的 leg 是结构化对象：detail 没给出交通方式时用它兜底。
      if (item.leg && typeof item.leg === "object" && !Array.isArray(item.leg)) {
        const legMode = item.leg.mode == null ? "" : String(item.leg.mode).trim();
        if (legMode) {
          const legLine = ISSUE_MODE_LABELS[legMode] || `交通方式：${legMode}`;
          if (!lines.includes(legLine)) push(legLine);
        }
      }
      return lines;
    },

    /** 按钮统一入口：需要重查的写进修订原话并复用 startRevise。 */
    runIssueAction(item, act) {
      if (!act) return;
      const key = String(act.key || "");
      if (key === "view_opening") {
        this.toggleOpening(item);
        return;
      }
      if (key === "retry_lodging_edit" || key === "retry_lodging") {
        // 只改参考地点文本：打开补充弹窗，等用户提交。
        this.retryLodging(item);
        return;
      }
      // retry_lodging_now 等其余动作都走既有修订通道（mode="revise"）。
      // 重新定位若仍失败，不在这里做「已定位」乐观提示，一切等新的 final。
      this.runIssueRevise(act.text);
    },

    /** 明细项的稳定 key：用于 v-for 与「查看开放依据」的展开状态。 */
    issueKey(item) {
      return `${(item && item.code) || ""}|${(item && item.ref) || ""}`;
    },

    /**
     * 「查看开放依据」：只在前端从 quality.sources 里找相关来源，不发任何请求。
     * 匹配优先级（后端新契约优先，旧快照兜底）：
     *   1) 来源行的 poi_ids 含本项的 poi_id（后端新字段，网页来源与景点 ID 的唯一正确关联）；
     *   2) 来源行 facts 里有 poi_id 相同、且日期可比的条目（item.date 与 fact.applies_on 都存在时才比日期）；
     *   3) 旧格式兜底：row.ref === issue.ref / row.ref === issue.poi_id / row.records[].id === issue.poi_id。
     * 同一来源命中多个景点时全部保留（按 poi_id 去重，不取第一个）。
     * 找不到就返回空数组，模板显示「尚无可用依据」。
     */
    openingSources(item) {
      const quality = this.quality || {};
      const rows = quality.sources || [];
      if (!item || typeof item !== "object") return [];
      const ref = String(item.ref || "").trim();
      const poiId = String(item.poi_id || "").trim();
      const itemDate = String(item.date || "").trim();
      if (!ref && !poiId) return [];
      const hits = [];
      const seenPoi = new Set();
      const quoteOf = (row) => {
        const rowQuote = String(row.quote || "").trim();
        if (rowQuote) return rowQuote;
        for (const record of row.records || []) {
          if (record && record.quote) return String(record.quote).trim();
        }
        return "";
      };
      const factKind = (kind) => {
        const key = String(kind || "").trim();
        if (!key) return "";
        return OPENING_KIND_LABELS[key] || key;
      };
      // 日期可比才比较，缺日期视为「可用于该景点」。
      const factDateOk = (fact) => {
        const factDate = String((fact && fact.applies_on) || "").trim();
        if (itemDate && factDate && itemDate !== factDate) return false;
        return true;
      };
      const pushHit = (row, fact) => {
        const hitPoi = String((fact && fact.poi_id) || poiId || "").trim();
        // 同一来源里同一景点只保留一条，避免重复堆叠。
        const dedupeKey = `${String(row.ref || "").trim()}|${hitPoi || String(hits.length)}`;
        if (seenPoi.has(dedupeKey)) return;
        seenPoi.add(dedupeKey);
        let quote = fact && fact.quote ? String(fact.quote).trim() : "";
        if (!quote) quote = quoteOf(row);
        hits.push({
          key: `${String(row.ref || "")}|${String(row.url || "")}|${hits.length}`,
          url: String(row.url || ""),
          label: String(row.label || row.ref || "来源"),
          quote,
          kind: factKind(fact && fact.kind),
          applies_on: String((fact && fact.applies_on) || "").trim(),
          queried_at: String(row.queried_at || ""),
        });
      };
      for (const row of rows) {
        if (!row) continue;
        const rowRef = String(row.ref || "").trim();
        // 1) 后端新契约：来源行直接给出关联的景点 ID 列表
        if (poiId && Array.isArray(row.poi_ids) && row.poi_ids.some((id) => String(id || "").trim() === poiId)) {
          const matchedFacts = (row.facts || []).filter((one) => one && String(one.poi_id || "").trim() === poiId);
          if (matchedFacts.length) {
            // 有明细事实时按日期过滤；日期全不符就不给依据，别拿别的日期的规则冒充。
            for (const fact of matchedFacts) {
              if (factDateOk(fact)) pushHit(row, fact);
            }
          } else {
            pushHit(row, null);
          }
          continue;
        }
        // 2) 后端新契约：来源事实里带 poi_id（+ 可选适用日期）
        if (poiId && Array.isArray(row.facts)) {
          const matchedFacts = row.facts.filter((one) => one && String(one.poi_id || "").trim() === poiId && factDateOk(one));
          if (matchedFacts.length) {
            for (const fact of matchedFacts) pushHit(row, fact);
            continue;
          }
        }
        // 3) 旧快照兜底：ref / records 直接对得上
        let matched = false;
        if (ref && rowRef === ref) matched = true;
        if (!matched && poiId && rowRef === poiId) matched = true;
        if (!matched && poiId) {
          for (const record of row.records || []) {
            if (record && String(record.id || "").trim() === poiId) {
              matched = true;
              break;
            }
          }
        }
        if (!matched) continue;
        pushHit(row, null);
      }
      return hits;
    },

    /** 展开 / 收起某一项的开放依据（纯前端状态）。 */
    toggleOpening(item) {
      const key = this.issueKey(item);
      this.openingIssueKey = this.openingIssueKey === key ? "" : key;
    },

    /**
     * 明细项按钮统一入口：先写进修订框，再走既有 startRevise（mode="revise"）。
     * 不新增请求字段、不新增接口，后端靠原话关键词识别刷新票务 / 重排路线。
     */
    runIssueRevise(text) {
      const q = String(text || "").trim();
      if (!q) return;
      if (!this.lastPlanId) return;
      this.reviseQuery = q;
      this.startRevise();
    },

    /** 「修改参考地点」：复用结果页确认弹窗，走既有 mode="confirm"（不直接触发重新定位）。 */
    retryLodging(item) {
      if (!this.lastSlots) return;
      const options = (item && Array.isArray(item.options) ? item.options : []).filter((it) => typeof it === "string" && it.trim());
      this.openConfirmFromResult(options.length ? { lodging: options[0].trim() } : null);
    },

    closeConfirm() {
      this.confirmOpen = false;
      if (this.confirmSource === "clarify") {
        // 澄清没提交就走开：草稿留着，但不能再把整页输入框、开始按钮和示例都锁死。
        // 状态改为「已暂时收起」，页面恢复可编辑，同时留「继续补充」入口重开弹窗。
        this.awaitingClarify = false;
        this.clarifyDeferred = true;
      }
    },

    /** 「继续补充」：把暂时收起的澄清草稿重新打开，不重新请求。 */
    resumeClarify() {
      if (!this.clarifyDeferred) return;
      this.confirmSource = "clarify";
      this.clarifyDeferred = false;
      this.awaitingClarify = true;
      this.confirmOpen = true;
      this.$nextTick(() => this.focusConfirm());
    },

    collectClarifySlots() {
      const daysNum = Number(this.clarifyForm.days);
      return {
        origin: String(this.clarifyForm.origin || "").trim(),
        destination: String(this.clarifyForm.destination || "").trim(),
        date: String(this.clarifyForm.date || "").trim(),
        days: Number.isFinite(daysNum) && daysNum >= 1 ? daysNum : 1,
        budget: String(this.clarifyForm.budget || "").trim() || "中等",
        preferences: String(this.clarifyForm.preferences || "").trim() || "经典景点 + 少折腾",
        pace: String(this.clarifyForm.pace || "").trim() || "舒适型节奏",
        lodging: String(this.clarifyForm.lodging || "").trim() || null,
        must_visit: nameList(this.clarifyForm.must_visit),
        avoid: nameList(this.clarifyForm.avoid),
        transport_mode: this.clarifyForm.transport_mode || "public_transit",
      };
    },

    buildPlanRequest(source) {
      /** 三个提交来源（clarify / supplement / revise）共用的请求组装。 */
      const slots = this.collectClarifySlots();
      const partyNum = Number(this.clarifyForm.party_size);
      const body = { slots };
      const party = Number.isFinite(partyNum) && partyNum >= 1 ? Math.round(partyNum) : null;
      const lodgingScope = this.clarifyForm.lodging_scope || null;
      const budgetScope = this.clarifyForm.budget_scope || null;
      const baseline = this._confirmBaseline || {};
      const cleared = [];
      if (party) body.party_size = party;
      if (lodgingScope) body.lodging_scope = lodgingScope;
      if (budgetScope) body.budget_scope = budgetScope;
      for (const key of ["party_size", "budget_scope", "lodging_scope"]) {
        if (baseline[key] && !this.clarifyForm[key]) cleared.push(key);
      }
      if (cleared.length) body.cleared_fields = cleared;
      if (this.clarifyForm.intercity_mode && this.clarifyForm.intercity_mode !== baseline.intercity_mode) {
        body.intercity_mode = this.clarifyForm.intercity_mode;
      }
      if (this.clarifyForm.return_required !== "" && this.clarifyForm.return_required !== baseline.return_required) {
        body.return_required = this.clarifyForm.return_required === "true";
      }
      const ctx = this.reqCtx || {};
      if (source === "clarify") {
        // 澄清提交：沿用触发澄清的那次请求上下文（query + 父方案 ID），避免拿错父方案或残留上次修改意见。
        // 修订触发的澄清 → confirm + 原修改意见 + 父方案 ID；首次规划澄清 → plan。
        body.query = String(ctx.query || this.query || "").trim();
        body.intent_source = ctx.intentSource || "initial";
        if (ctx.parentPlanId) {
          body.mode = "confirm";
          body.plan_id = ctx.parentPlanId;
        } else {
          body.mode = "plan";
        }
      } else if (source === "supplement") {
        // 补充提交：带上一版方案，让后端按「改了什么」重判分支。
        body.query = this.lastBaseQuery || this.query.trim();
        body.mode = "confirm";
        body.intent_source = "background";
        if (this.lastPlanId) body.plan_id = this.lastPlanId;
      } else {
        body.query = (this.reviseQuery || "").trim();
        body.mode = "revise";
        body.intent_source = "revision";
        if (this.lastPlanId) body.plan_id = this.lastPlanId;
      }
      return body;
    },

    async startRevise() {
      /** 修订：query 是修改意见，plan_id 指向上一份方案；不再回传整份旧槽位 */
      const q = (this.reviseQuery || "").trim();
      if (!q || !this.lastPlanId || this.loading) return;
      // 发起新请求前先作废旧一轮的待确认清单，避免旧文案残留到新方案。
      this.confirmPending = [];
      if (this.abortController) this.abortController.abort();
      const controller = new AbortController();
      this.abortController = controller;
      const runId = ++this.runSeq;
      this.activeRunId = runId;
      this.loading = true;
      this.steps = emptySteps();
      this.closedEarly = false;
      this.errorText = "";
      this.warnings = [];
      this.finalReady = false;
      this.quality = null;
      this.finalText = "";
      this.renderedHtml = "";
      this.prepareRunUi();
      // 记录本轮请求上下文：澄清提交要沿用这份 query + 父方案 ID。
      this.reqCtx = { query: q, parentPlanId: this.lastPlanId, intentSource: "revision" };
      try {
        await this.consumeSse({ query: q, plan_id: this.lastPlanId, mode: "revise", intent_source: "revision" }, controller.signal, runId);
      } catch (err) {
        if (runId !== this.activeRunId) return;
        if (err && err.name === "AbortError") return;
        this.errorText = err && err.message ? err.message : String(err);
      } finally {
        this.endRun(controller, runId);
      }
    },

    async startPlan() {
      /** 「开始规划」：只带自然语言 query */
      if (!this.canSubmit) return;
      if (this.abortController) this.abortController.abort();
      const controller = new AbortController();
      this.abortController = controller;
      const runId = ++this.runSeq;
      this.activeRunId = runId;
      this.loading = true;
      this.resetPlanState();
      this.lastBaseQuery = this.query.trim();
      this.prepareRunUi();
      // 首次规划：请求上下文无父方案，澄清提交时走 plan 续提。
      this.reqCtx = { query: this.query.trim(), parentPlanId: "", intentSource: "initial" };
      try {
        await this.consumeSse({ query: this.query.trim(), intent_source: "initial" }, controller.signal, runId);
      } catch (err) {
        if (runId !== this.activeRunId) return;
        if (err && err.name === "AbortError") return;
        this.errorText = err && err.message ? err.message : String(err);
      } finally {
        this.endRun(controller, runId);
      }
    },

    async confirmClarify() {
      /** 确认卡提交：带上已填槽位，后端不再因缺字段卡住 */
      if (!this.canConfirmClarify) return;
      await this.submitConfirm("clarify");
    },

    async submitConfirm(source) {
      if (this.loading) return;
      // 提交前清掉上一轮 clarify 的待确认清单（新请求的结果会重新算 resultPending）。
      this.confirmPending = [];
      if (this.abortController) this.abortController.abort();
      const controller = new AbortController();
      this.abortController = controller;
      const runId = ++this.runSeq;
      this.activeRunId = runId;
      this.awaitingClarify = false;
      this.clarifyDeferred = false;
      this.confirmOpen = false;
      this.loading = true;
      this.closedEarly = false;
      this.errorText = "";
      this.steps = emptySteps();
      this.warnings = [];
      this.finalReady = false;
      this.quality = null;
      this.finalText = "";
      this.renderedHtml = "";
      this.prepareRunUi();
      const body = this.buildPlanRequest(source);
      if (!this.lastBaseQuery) this.lastBaseQuery = String(body.query || "").trim();
      try {
        await this.consumeSse(body, controller.signal, runId);
      } catch (err) {
        if (runId !== this.activeRunId) return;
        if (err && err.name === "AbortError") return;
        this.errorText = err && err.message ? err.message : String(err);
      } finally {
        this.endRun(controller, runId);
      }
    },
  },

  mounted() {
    document.addEventListener("fullscreenchange", this.onFullscreenChange);
    window.addEventListener("scroll", this.onPageScroll, { passive: true });
  },

  beforeUnmount() {
    document.removeEventListener("fullscreenchange", this.onFullscreenChange);
    window.removeEventListener("scroll", this.onPageScroll);
    // 先作废请求身份，再 abort：abort 触发的旧请求收尾不会再去动已卸载组件。
    this.activeRunId = null;
    if (this.abortController) this.abortController.abort();
    if (this._renderTimer) clearTimeout(this._renderTimer);
  },
}).mount("#app");
