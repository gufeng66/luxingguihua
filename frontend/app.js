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

/** 左侧进度条四步（id 必须和后端 step 事件一致） */
const STEP_DEFS = [
  { id: "understand", label: "理解需求" },
  { id: "ticket", label: "查票" },
  { id: "map", label: "找景点" },
  { id: "summary", label: "汇总" },
];

const CRITICAL_KEYS = ["origin", "destination", "date"];
const SLOT_LABELS = {
  origin: "出发地",
  destination: "目的地",
  date: "出行日期",
  days: "游玩天数",
  budget: "预算",
  preferences: "偏好",
  pace: "出行节奏",
  lodging: "住宿地",
};


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

createApp({
  data() {
    return {
      query: "",
      loading: false,
      steps: emptySteps(),
      finalText: "",
      renderedHtml: "",
      errorText: "",
      warningText: "",
      savedUrl: "",
      mapUrl: "",
      traceUrl: "",
      timings: null,
      lastPlanId: "",
      lastSlots: null,
      reviseQuery: "",
      abortController: null,
      awaitingClarify: false,
      clarifyForm: {
        origin: "",
        destination: "",
        date: "",
        days: 1,
        budget: "",
        preferences: "",
        pace: "",
        lodging: "",
      },
      missingFields: [],
      defaultsApplied: [],
      _renderTimer: null,
      _lastRenderAt: 0,
      demoChips: DEMO_CHIPS,
      chipHint: "",
      sampleNote: "",
      copyLabel: "复制",
      resultTab: "plan",
      processExpanded: false,
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
    queryLen() {
      return this.query.length;
    },
    statusLabel() {
      if (this.loading || this.awaitingClarify) return "规划中";
      if (this.sampleNote) return "示例";
      if (this.finalText || this.errorText) return "已完成";
      return "空闲";
    },
    statusBadgeClass() {
      if (this.loading || this.awaitingClarify) return "is-busy";
      if (this.finalText || this.errorText) return "is-done";
      return "is-idle";
    },
    processSummary() {
      const started = this.steps.filter((s) => s.status !== "pending").length;
      const sec = this.processSeconds;
      if (this.processExpanded) {
        return this.loading ? "规划中 · 点击收起" : "点击收起";
      }
      if (!started && !this.loading) return "尚未开始 · 点击展开";
      const n = started || this.steps.length;
      if (sec != null) return `${n} 个步骤 · 共 ${sec}s · 点击展开`;
      return `${n} 个步骤 · 点击展开`;
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
    processSeconds() {
      if (this.timings && this.timings.total_ms != null) {
        return (this.timings.total_ms / 1000).toFixed(1);
      }
      let ms = 0;
      let has = false;
      for (const step of this.steps) {
        if (step.elapsedMs != null) {
          ms += step.elapsedMs;
          has = true;
        }
      }
      return has ? (ms / 1000).toFixed(1) : null;
    },
  },

  watch: {
    renderedHtml() {
      this.$nextTick(() => this.autoScrollResult());
    },
    loading(val) {
      if (!val && !this.awaitingClarify) this.processExpanded = false;
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
      this.scheduleRender(true);
    },

    async copyFinal() {
      const text = this.finalText || "";
      if (!text) return;
      try {
        await navigator.clipboard.writeText(text);
        this.copyLabel = "已复制";
        setTimeout(() => {
          this.copyLabel = "复制";
        }, 1500);
      } catch (_e) {
        this.copyLabel = "复制失败";
        setTimeout(() => {
          this.copyLabel = "复制";
        }, 1500);
      }
    },

    isNarrowViewport() {
      return window.matchMedia("(max-width: 900px)").matches;
    },

    prepareRunUi() {
      this.resultTab = "plan";
      this.resultUserScrolled = false;
      this.processExpanded = !this.isNarrowViewport();
    },

    formatSeconds(ms) {
      if (ms == null || Number.isNaN(Number(ms))) return "—";
      return (Number(ms) / 1000).toFixed(1);
    },

    toggleProcess() {
      this.processExpanded = !this.processExpanded;
    },

    onResultScroll(event) {
      const el = event.target;
      const gap = el.scrollHeight - el.scrollTop - el.clientHeight;
      this.resultUserScrolled = gap > 48;
    },

    autoScrollResult() {
      const el = this.$refs.resultBody;
      if (!el || this.resultUserScrolled) return;
      el.scrollTop = el.scrollHeight;
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
      this.warningText = "";
      this.savedUrl = "";
      this.mapUrl = "";
      this.traceUrl = "";
      this.timings = null;
      this.reviseQuery = "";
      this.lastPlanId = "";
      this.lastSlots = null;
      this.awaitingClarify = false;
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
        this.renderedHtml = DOMPurify.sanitize(marked.parse(this.finalText));
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

    handleEvent(event) {
      /** 消化一帧 SSE：改进度条、确认卡、打字机正文或最终结果 */
      const type = event.type;

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
        this.loading = false;
        this.missingFields = event.missing || [];
        this.defaultsApplied = event.defaults_applied || [];
        const s = event.slots || {};
        this.clarifyForm = {
          origin: s.origin || "",
          destination: s.destination || "",
          date: s.date || "",
          days: s.days != null ? s.days : 1,
          budget: s.budget || "",
          preferences: s.preferences || "",
          pace: s.pace || "",
          lodging: s.lodging || "",
        };
        return;
      }
      if (type === "warning") {
        this.warningText = event.message || "出现告警";
        return;
      }
      if (type === "slots") {
        this.lastSlots = event.slots || null;
        return;
      }
      if (type === "token") {
        this.finalText += event.content || "";
        this.scheduleRender(false);
        return;
      }
      if (type === "final") {
        this.finalText = event.content || this.finalText;
        this.savedUrl = event.saved_url || "";
        this.mapUrl = event.map_url || "";
        this.traceUrl = event.trace_url || "";
        this.lastPlanId = event.plan_id || "";
        this.timings = event.timings || null;
        this.resultUserScrolled = true;
        this.scheduleRender(true);
        this.$nextTick(() => {
          const el = this.$refs.resultBody;
          if (el) el.scrollTop = 0;
        });
        this.loadMapMeta();
        return;
      }
      if (type === "error") {
        this.errorText = event.message || "规划失败";
      }
    },

    async consumeSse(body, signal) {
      /** POST /api/plan，按空行拆 SSE 帧，解析 data: 后的 JSON */
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
            this.handleEvent(JSON.parse(jsonText));
          } catch (_e) {
            /* 跳过坏帧 */
          }
        }
      }
    },

    async startRevise() {
      /** 修订：query 是修改意见，plan_id 指向上一份方案 */
      const q = (this.reviseQuery || "").trim();
      if (!q || !this.lastPlanId || this.loading) return;
      if (this.abortController) this.abortController.abort();
      this.abortController = new AbortController();
      this.loading = true;
      this.errorText = "";
      this.warningText = "";
      this.steps = emptySteps();
      this.finalText = "";
      this.renderedHtml = "";
      this.prepareRunUi();
      try {
        await this.consumeSse(
          { query: q, plan_id: this.lastPlanId, slots: this.lastSlots },
          this.abortController.signal
        );
      } catch (err) {
        if (err && err.name === "AbortError") return;
        this.errorText = err && err.message ? err.message : String(err);
      } finally {
        if (!this.awaitingClarify) this.loading = false;
      }
    },

    async startPlan() {
      /** 「开始规划」：只带自然语言 query */
      if (!this.canSubmit) return;
      if (this.abortController) this.abortController.abort();
      this.abortController = new AbortController();
      this.loading = true;
      this.resetPlanState();
      this.prepareRunUi();
      try {
        await this.consumeSse({ query: this.query.trim() }, this.abortController.signal);
      } catch (err) {
        if (err && err.name === "AbortError") return;
        this.errorText = err && err.message ? err.message : String(err);
      } finally {
        if (!this.awaitingClarify) this.loading = false;
      }
    },

    async confirmClarify() {
      /** 确认卡提交：带上已填槽位，后端不再因缺字段卡住 */
      if (!this.canConfirmClarify) return;
      if (this.abortController) this.abortController.abort();
      this.abortController = new AbortController();
      this.awaitingClarify = false;
      this.loading = true;
      this.errorText = "";
      this.steps = emptySteps();
      this.finalText = "";
      this.renderedHtml = "";
      this.prepareRunUi();
      const daysNum = Number(this.clarifyForm.days);
      const slots = {
        origin: String(this.clarifyForm.origin || "").trim(),
        destination: String(this.clarifyForm.destination || "").trim(),
        date: String(this.clarifyForm.date || "").trim(),
        days: Number.isFinite(daysNum) && daysNum >= 1 ? daysNum : 1,
        budget: String(this.clarifyForm.budget || "").trim() || "中等",
        preferences: String(this.clarifyForm.preferences || "").trim() || "经典景点 + 少折腾",
        pace: String(this.clarifyForm.pace || "").trim() || "舒适型节奏",
        lodging: String(this.clarifyForm.lodging || "").trim() || null,
      };
      try {
        await this.consumeSse(
          { query: this.query.trim(), slots },
          this.abortController.signal
        );
      } catch (err) {
        if (err && err.name === "AbortError") return;
        this.errorText = err && err.message ? err.message : String(err);
      } finally {
        if (!this.awaitingClarify) this.loading = false;
      }
    },
  },

  mounted() {
    document.addEventListener("fullscreenchange", this.onFullscreenChange);
  },

  beforeUnmount() {
    document.removeEventListener("fullscreenchange", this.onFullscreenChange);
    if (this.abortController) this.abortController.abort();
    if (this._renderTimer) clearTimeout(this._renderTimer);
  },
}).mount("#app");
