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

const CROSS_CITY_QUERY = "这周六从新乡坐高铁去郑州一日游，预算 500，少折腾";
const DRIVE_QUERY = "下周从新乡租电车自驾去洛阳两天，轻松点，别安排开封和宝泉";
const REVISE_QUERY = "不要龙门石窟，改白马寺，路线顺一点";
const REVISE_STANDALONE =
  "下周从新乡租电车自驾去洛阳两天，轻松点，别安排开封和宝泉，不要龙门石窟改白马寺，路线顺一点";
const SAMPLE_NOTE =
  "示例方案，未查询实时票务与地图。点「开始规划」会按当前输入重新生成。";

/** 写好的演示稿。车次用时间窗，不编车次号。 */
const RAIL_PLAN = `**推荐：** 郑州东进出，地铁 1 号线直达二七，老城步行一圈后原路返回。不跨区、不换乘。

## 需求摘要

- 出行：2026-10-03（周六），新乡 → 郑州，一日往返
- 方式：高铁；市内只坐地铁 1 号线
- 预算：500 元以内（按 1 人）
- 节奏：少折腾。主方案不换线、不出城

## 景点建议

**主方案（一条线）**

郑州东站下车后，地铁 1 号线直达二七广场。下面几处都在步行范围内，不用再坐车：

1. 二七纪念塔：看外观和广场即可，约 20 分钟
2. 德化步行街：午饭、买伴手礼，约 1.5 小时
3. 城隍庙：闲逛或吃小吃，约 40 分钟
4. 郑州商城遗址（商代城墙段）：就在附近，约 40 分钟

傍晚从二七广场坐 1 号线回郑州东站。

**备选（愿意多换一次地铁再加）**

上午改去河南博物院：1 号线到紫荆山，换乘 2 号线到关虎屯，步行到馆，主展厅约 2.5 小时，须提前预约。下午若累了，取消二七，直接回郑州东等车。周六预约紧，没约上就退回主方案。

**不安排**

黄河风景区、少林寺、方特。都要出城，和「少折腾」冲突。

## 车票建议

下面是选车规则，不是 12306 实时余票，没有具体车次号。

- 去程：7:30–8:30，新乡东 → 郑州东。车程大约 20–40 分钟，二等座单程按 20–40 元估
- 返程：17:00–19:00，郑州东 → 新乡东
- 只选新乡东、郑州东对发。不要新乡站或郑州站，否则市内还要再倒一次

## 预算

| 项目 | 金额（元） |
| --- | --- |
| 高铁往返 | 80 |
| 地铁往返 | 8 |
| 门票 | 0（主方案室外景点） |
| 午饭烩面 + 晚饭 | 90 |
| 水、小吃 | 30 |
| 机动 | 40 |
| 合计 | 约 250，低于 500 |

两人时高铁和餐费按人数加。购物另计，不进这 500。

## 行程表

| 时间 | 安排 |
| --- | --- |
| 07:40 前后 | 新乡东上车，郑州东下车 |
| 08:30 | 地铁 1 号线到二七广场 |
| 09:00–09:30 | 二七纪念塔 |
| 09:40–11:30 | 德化街，午饭 |
| 13:00–14:00 | 城隍庙 |
| 14:10–15:00 | 郑州商城遗址 |
| 15:30 | 地铁回郑州东，站内休息 |
| 17:00–19:00 | 高铁返新乡，到家大约 19:30 前 |

## 注意事项

- 周六二七人多，塔下拍照 20 分钟就走
- 博物院周一闭馆；改走备选须前一天预约
- 正式车票以 12306 当天余票为准
`;

const RAIL_PLAN_NO_TICKET = `**推荐：** 景点仍按「郑州东进出、地铁 1 号线到二七、老城步行」来排。车票这一节没有数据，不能当成订票依据。

## 需求摘要

- 出行：2026-10-03（周六），新乡 → 郑州，一日往返，预算 500，少折腾
- 本次车票服务不可用，只保留景点与花费结构

## 景点建议

**主方案（一条线）**

到郑州东后，地铁 1 号线直达二七广场，步行串完，不再换线：

1. 二七纪念塔，约 20 分钟
2. 德化步行街，午饭，约 1.5 小时
3. 城隍庙，约 40 分钟
4. 郑州商城遗址，约 40 分钟

**备选**

河南博物院：1 号线紫荆山换乘 2 号线关虎屯，主展厅约 2.5 小时，须预约。没约上或觉得折腾，就只走主方案。

**不安排**

黄河风景区、少林寺、方特。

## 车票建议

车票服务不可用，没有可靠票务数据。不提供车次号、票价和发车时刻。

请自行打开 12306，查 2026-10-03 新乡东 ↔ 郑州东。优先东站对发、上午去、傍晚回。不要选新乡站或郑州站，以免多倒一次市内交通。

## 预算

车票未知，下表高铁按 80 元占位，查到实价后替换。其余按主方案估算。

| 项目 | 金额（元） |
| --- | --- |
| 高铁往返 | 待查（暂按 80） |
| 地铁往返 | 8 |
| 门票 | 0 |
| 两餐 + 小吃 | 120 |
| 机动 | 40 |
| 合计 | 车票落地后大约 250 上下，仍低于 500 |

## 行程表

钟点假设上午 8:30 前到郑州东。车次未定之前，只看先后顺序。

| 顺序 | 安排 |
| --- | --- |
| 1 | 新乡东 → 郑州东 |
| 2 | 地铁 1 号线到二七广场 |
| 3 | 二七纪念塔 → 德化街午饭 → 城隍庙 → 商城遗址 |
| 4 | 地铁回郑州东 |
| 5 | 傍晚高铁返新乡 |

## 注意事项

- 服务恢复前不要按某一班车卡点
- 博物院改备选时前一天预约
- 购物不进 500 元预算
`;

const DRIVE_PLAN = `**推荐：** 两天都住洛阳老城。第一天少开车逛洛邑古城；第二天上午龙门石窟，下午返新乡。不去开封，不去宝泉。

## 需求摘要

- 出行：2026-10-05 至 2026-10-06（示意「下周」两天）
- 路线：新乡租电车自驾洛阳，同城取还
- 节奏：轻松。老城步行，不开夜车
- 排除：开封、宝泉

## 景点建议

**第 1 天 · 老城，车停酒店**

下午到达后只逛能走路串起来的几处：丽景门、洛邑古城、十字街。晚饭在古城解决，不把车开去夜市。

**第 2 天 · 龙门后返程**

上午龙门石窟，只走西山石窟主线，约 3 小时，不加东山。中午在景区外简餐，下午上连霍回新乡。龙门在城南，返程要先北上再进高速，这是这条方案里唯一绕路的一段。

**备选**

龙门预约已满或队伍过长：改关林（洛阳城南、顺路程度仍一般），或把第二天上午留在老城，提前返程。

**不安排**

开封（反向，多半天车程）、宝泉（新乡本地，和这次目的地无关）。

## 车票建议

本次未查票（无需铁路）。不要编高铁车次。

- 新乡 — 洛阳走连霍，约 160 公里，约 2 小时
- 电车按续航 400 公里示意：去程约 160 公里，市内短途另计
- 新乡取车时充满；老城酒店过夜慢充一次；返程前电量不要低于 30%
- 还车点选新乡。不要异地还到洛阳

## 预算

单人、电车两人分担租金时的示意，不是报价。

| 项目 | 金额（元） |
| --- | --- |
| 租车 2 天（两人分摊后） | 300 |
| 过路费往返 | 80 |
| 电费 | 60 |
| 老城一晚 | 280 |
| 龙门门票 | 90 |
| 两天餐食 | 200 |
| 合计 | 约 1010 |

## 行程表

| 时间 | 安排 |
| --- | --- |
| 第 1 天 08:30 | 新乡取车，电量 100% |
| 第 1 天 11:00 前后 | 到洛阳，车停老城酒店并挂充电 |
| 第 1 天 14:00–18:00 | 丽景门、洛邑古城、十字街 |
| 第 1 天 晚上 | 古城晚饭，不挪车 |
| 第 2 天 08:30–12:00 | 龙门石窟西山 |
| 第 2 天 13:00 | 简餐，确认电量后上连霍 |
| 第 2 天 16:00 前后 | 新乡还车 |

## 注意事项

- 龙门须提前预约，周六日更紧；约不上就走备选，不要在门口硬等
- 老城巷子窄，导航到酒店停车场为止，景区内步行
- 开封、宝泉不要临时加进去
`;

const REVISE_PLAN = `**推荐：** 去程先停白马寺（洛阳东侧，下高速顺路），再进老城住一晚；第二天只在老城步行，下午直接上连霍回家。不再南下龙门。

## 需求摘要

- 在「新乡租电车、洛阳两天、轻松、不去开封和宝泉」上修订
- 修订：不要龙门石窟，改白马寺，路线顺一点
- 日期示意：2026-10-05 至 2026-10-06
- 未改的约束：天数、排除开封和宝泉、电车同城取还

## 景点建议

**第 1 天 · 顺路白马寺，晚上老城**

连霍从新乡方向进洛阳走东边。先到白马寺，参观约 2 小时（齐云塔、大雄殿一带，不赶时间）。然后开车进老城，车停酒店，下午到晚上只步行：丽景门、洛邑古城、十字街。

**第 2 天 · 老城收尾，直接返程**

上午仍在老城：十字街早饭，丽景门和古城里没走完的巷子，约 2 小时。午饭后上连霍回新乡。不去城南。

相对上一版，删掉龙门西山这趟南下再北返，少开大约 1 小时空路。

**备选**

白马寺闭馆或人太多：当天改停洛邑古城，把白马寺挪到第二天上午、返程前顺路再去一次（仍在东边，不南下）。

**不安排**

龙门石窟、关林、开封、宝泉。

## 车票建议

本次未查票（无需铁路），修订也没有改成坐火车。

- 里程仍约 160 公里、约 2 小时，另加白马寺到老城的市区短途
- 取车充满；白马寺不作为充电点，充电放在老城酒店过夜
- 返程前电量不低于 30%，还车在新乡

## 预算

龙门门票去掉，换成白马寺门票。其余沿用上一版口径（单人、租金两人分摊，示意）。

| 项目 | 金额（元） |
| --- | --- |
| 租车 2 天（两人分摊后） | 300 |
| 过路费往返 | 80 |
| 电费 | 50 |
| 老城一晚 | 280 |
| 白马寺门票 | 35 |
| 两天餐食 | 200 |
| 合计 | 约 945 |

## 行程表

| 时间 | 安排 |
| --- | --- |
| 第 1 天 08:30 | 新乡取车，电量 100% |
| 第 1 天 10:40–12:40 | 白马寺 |
| 第 1 天 13:30 | 老城酒店，停车充电，午饭 |
| 第 1 天 15:00–18:30 | 丽景门、洛邑古城、十字街 |
| 第 2 天 09:00–11:30 | 老城补完步行 |
| 第 2 天 12:30 | 午饭，确认电量 |
| 第 2 天 15:30 前后 | 新乡还车 |

## 注意事项

- 白马寺在洛阳东，不要按导航先穿城到龙门
- 老城只步行，车留在酒店
- 开封、宝泉仍然排除
`;

/** 演示剧本：有 plan 的条目会展示写好的方案，不替用户发请求 */
const DEMO_CHIPS = [
  { id: "rail", label: "跨城高铁", query: CROSS_CITY_QUERY, mode: "query", plan: RAIL_PLAN },
  { id: "drive", label: "租电车自驾", query: DRIVE_QUERY, mode: "query", plan: DRIVE_PLAN },
  { id: "hitl", label: "HITL 确认卡", query: "周末去郑州玩", mode: "query" },
  {
    id: "mcp",
    label: "MCP 降级",
    query: CROSS_CITY_QUERY,
    mode: "query",
    plan: RAIL_PLAN_NO_TICKET,
    hint: "上面是服务不可用时的成文。要复现真实降级，把 .env 里 MCP_12306_URL 改成无效地址后再点开始规划",
  },
  { id: "revise", label: "修订方案", query: REVISE_QUERY, mode: "revise", plan: REVISE_PLAN },
];

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
      events: [],
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
      return CRITICAL_KEYS.every((k) => String(this.clarifyForm[k] || "").trim());
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
      this.events = [];
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
