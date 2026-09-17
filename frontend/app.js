/**
 * 旅游规划多智能体 — 前端逻辑（Vue3 CDN）
 *
 * 模板在 index.html；本文件负责状态、SSE 与交互。
 * 安全：前端永不持有 API Key。
 */

const { createApp } = Vue;

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
};

function emptySteps() {
  return STEP_DEFS.map((s) => ({
    id: s.id,
    label: s.label,
    status: "pending",
    tools: [],
  }));
}

createApp({
  data() {
    return {
      query: "",
      loading: false,
      events: [],
      steps: emptySteps(),
      finalText: "",
      renderedHtml: "",
      errorText: "",
      savedUrl: "",
      mapUrl: "",
      traceUrl: "",
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
      },
      missingFields: [],
      defaultsApplied: [],
      _renderTimer: null,
      _lastRenderAt: 0,
      sample:
        "帮我规划下周六从杭州去苏州一日游，预算 800，想看园林和老街，尽量少折腾",
    };
  },

  computed: {
    canSubmit() {
      const len = this.query.trim().length;
      return !this.loading && !this.awaitingClarify && len > 0 && len <= 300;
    },
    canConfirmClarify() {
      if (this.loading) return false;
      return CRITICAL_KEYS.every((k) => String(this.clarifyForm[k] || "").trim());
    },
    queryLen() {
      return this.query.length;
    },
  },

  methods: {
    fillSample() {
      this.query = this.sample;
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
      this.events = [];
      this.steps = emptySteps();
      this.finalText = "";
      this.renderedHtml = "";
      this.errorText = "";
      this.savedUrl = "";
      this.mapUrl = "";
      this.traceUrl = "";
      this.awaitingClarify = false;
      this.missingFields = [];
      this.defaultsApplied = [];
    },

    applyStepEvent(event) {
      const step = this.steps.find((s) => s.id === event.id);
      if (step) step.status = event.status;
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
      this.events.push(event);
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
          const cur = this.currentActiveStep();
          if (cur && cur.id !== id && cur.id !== "understand") {
            cur.status = "done";
          }
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
        };
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
        this.scheduleRender(true);
        return;
      }
      if (type === "error") {
        this.errorText = event.message || "规划失败";
      }
    },

    async consumeSse(body, signal) {
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

    async startPlan() {
      if (!this.canSubmit) return;
      if (this.abortController) this.abortController.abort();
      this.abortController = new AbortController();
      this.loading = true;
      this.resetPlanState();
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
      if (!this.canConfirmClarify) return;
      if (this.abortController) this.abortController.abort();
      this.abortController = new AbortController();
      this.awaitingClarify = false;
      this.loading = true;
      this.errorText = "";
      this.steps = emptySteps();
      this.finalText = "";
      this.renderedHtml = "";
      const daysNum = Number(this.clarifyForm.days);
      const slots = {
        origin: String(this.clarifyForm.origin || "").trim(),
        destination: String(this.clarifyForm.destination || "").trim(),
        date: String(this.clarifyForm.date || "").trim(),
        days: Number.isFinite(daysNum) && daysNum >= 1 ? daysNum : 1,
        budget: String(this.clarifyForm.budget || "").trim() || "中等",
        preferences: String(this.clarifyForm.preferences || "").trim() || "经典景点 + 少折腾",
        pace: String(this.clarifyForm.pace || "").trim() || "舒适型节奏",
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

  beforeUnmount() {
    if (this.abortController) this.abortController.abort();
    if (this._renderTimer) clearTimeout(this._renderTimer);
  },
}).mount("#app");
