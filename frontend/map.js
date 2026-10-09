/**
 * 高德地图链接与 HTML 契约（无 Vue）。
 * 由 app.js 在父页打开预览 / App 链接，避免 iframe 沙箱吞掉弹窗。
 */

const AMAP_TRAVEL_PLAN_BASE =
  "https://a.amap.com/jsapi_demo_show/static/openclaw/travel_plan.html";
const AMAP_APP_SCHEMES = /^(iosamap|androidamap|amapuri|amap):/i;

function generateMapLink(tasks) {
  /** 高德 travel_plan 预览页：把 POI/路线 JSON 放进 URL 的 data 参数 */
  return `${AMAP_TRAVEL_PLAN_BASE}?data=${encodeURIComponent(JSON.stringify(tasks))}`;
}

function firstPoi(tasks) {
  /** 数组里第一个带合法经纬度的景点，用来拼「高德 App 看首站」 */
  if (!Array.isArray(tasks)) return null;
  return (
    tasks.find(
      (item) =>
        item &&
        item.type === "poi" &&
        Array.isArray(item.lnglat) &&
        item.lnglat.length >= 2 &&
        Number.isFinite(Number(item.lnglat[0])) &&
        Number.isFinite(Number(item.lnglat[1]))
    ) || null
  );
}

function generateAmapAppLink(poi) {
  /** https 链接，避免 iframe 里 iosamap:// 被拦 */
  if (!poi || !Array.isArray(poi.lnglat) || poi.lnglat.length < 2) return "";
  const lon = Number(poi.lnglat[0]);
  const lat = Number(poi.lnglat[1]);
  if (!Number.isFinite(lon) || !Number.isFinite(lat)) return "";
  const name = encodeURIComponent(poi.text || "首站");
  return `https://uri.amap.com/marker?position=${lon},${lat}&name=${name}&src=travel-planner&coordinate=gaode&callnative=1`;
}

function amapAppLinkFromHref(href, fallbackPoi) {
  const raw = String(href || "").trim();
  if (AMAP_APP_SCHEMES.test(raw)) {
    try {
      const url = new URL(raw);
      const lat = url.searchParams.get("lat");
      const lon = url.searchParams.get("lon") || url.searchParams.get("lng");
      const name =
        url.searchParams.get("poiname") ||
        url.searchParams.get("name") ||
        (fallbackPoi && fallbackPoi.text) ||
        "首站";
      if (lat && lon) {
        return generateAmapAppLink({ lnglat: [lon, lat], text: name });
      }
    } catch (_e) {
      /* 解析失败则走 POI 兜底 */
    }
  }
  return generateAmapAppLink(fallbackPoi);
}

function parseAmapTaskData(html) {
  /** 从地图 HTML 里读 <script id="amapTaskData"> 契约 JSON */
  if (!html) return null;
  try {
    const doc = new DOMParser().parseFromString(html, "text/html");
    const el = doc.getElementById("amapTaskData");
    if (!el) return null;
    const tasks = JSON.parse(el.textContent || "null");
    if (!Array.isArray(tasks)) return null;
    return tasks;
  } catch (_e) {
    return null;
  }
}

function isPlaceholderHref(href) {
  const raw = String(href || "").trim();
  if (!raw || raw === "#" || raw === "about:blank") return true;
  return raw.charAt(0) === "#" && raw.indexOf("://") === -1;
}

function isHttpUrl(href) {
  const raw = String(href || "").trim();
  if (isPlaceholderHref(raw) || AMAP_APP_SCHEMES.test(raw)) return false;
  try {
    const url = new URL(raw, window.location.href);
    return url.protocol === "http:" || url.protocol === "https:";
  } catch (_e) {
    return false;
  }
}

function escapeHtmlAttr(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;");
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function taskDayHint(item) {
  if (!item || typeof item !== "object") return 0;
  const raw = item.day != null ? item.day : item.sort;
  const n = Number(raw);
  if (Number.isFinite(n) && n >= 1) return Math.floor(n);
  const m = String(raw || "").match(/(?:day|d|第)\s*(\d+)/i) || String(raw || "").match(/(\d+)\s*日/);
  return m ? parseInt(m[1], 10) : 0;
}

function assignTaskDays(tasks) {
  if (!Array.isArray(tasks)) return [];
  let current = 1;
  return tasks.map((item) => {
    const hinted = taskDayHint(item);
    if (hinted >= 1) current = hinted;
    return Object.assign({}, item, { day: current });
  });
}

function listMapDays(tasks) {
  const days = [];
  for (const item of assignTaskDays(tasks)) {
    if (!days.includes(item.day)) days.push(item.day);
  }
  return days.sort((a, b) => a - b);
}

function filterTasksByDay(tasks, day) {
  const assigned = assignTaskDays(tasks);
  if (day == null || day === "all") return assigned;
  const n = Number(day);
  return assigned.filter((item) => item.day === n);
}

function _lnglatOf(item) {
  if (!item) return null;
  if (item.type === "poi" && Array.isArray(item.lnglat) && item.lnglat.length >= 2) {
    return [Number(item.lnglat[0]), Number(item.lnglat[1])];
  }
  return null;
}

function _pair(arr) {
  if (!Array.isArray(arr) || arr.length < 2) return null;
  const p = [Number(arr[0]), Number(arr[1])];
  return Number.isFinite(p[0]) && Number.isFinite(p[1]) ? p : null;
}

function dayColor(day) {
  const palette = ["#2a9d8f", "#c47a2c", "#3d5a80", "#9b4d7a", "#c45c4a"];
  const i = Math.max(1, Number(day) || 1) - 1;
  return palette[i % palette.length];
}

function coordKey(lnglat) {
  // ponytail: 1e-4°（约 11 米）内算同一点。同一站入口差更远时再放宽。
  if (!Array.isArray(lnglat) || lnglat.length < 2) return "";
  const lon = Number(lnglat[0]);
  const lat = Number(lnglat[1]);
  if (!Number.isFinite(lon) || !Number.isFinite(lat)) return "";
  return Math.round(lon * 1e4) + "," + Math.round(lat * 1e4);
}

function stampPoiLabels(tasks) {
  /** 按出现顺序编号。同一坐标只保留第一次的序号，起点与终点重合时起点仍是 1。 */
  const seen = Object.create(null);
  let n = 0;
  return (Array.isArray(tasks) ? tasks : []).map((item) => {
    if (!item || item.type !== "poi") return item;
    const key = coordKey(item.lnglat);
    if (!key) return item;
    if (seen[key] == null) {
      n += 1;
      seen[key] = n;
    }
    return Object.assign({}, item, { label: seen[key] });
  });
}

function buildMapViewerHtml(tasks, dayFilter) {
  const rows = stampPoiLabels(filterTasksByDay(tasks, dayFilter));
  const title = dayFilter == null || dayFilter === "all" ? "全程路线" : "第 " + dayFilter + " 天";
  const days = listMapDays(rows);
  const legend =
    days.length > 1
      ? days
          .map(function (d) {
            return '<span><i style="background:' + dayColor(d) + '"></i>第' + d + "天</span>";
          })
          .join("")
      : "";
  const json = JSON.stringify(rows);
  return (
    "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">" +
    '<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css">' +
    "<style>" +
    "html,body{height:100%;margin:0;overscroll-behavior:contain;font-family:'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif}" +
    ".bar{display:flex;align-items:center;gap:12px;padding:8px 12px;background:#0f3d38;color:#f3faf8;font-size:13px}" +
    ".bar strong{font-weight:600}" +
    ".legend{display:flex;flex-wrap:wrap;gap:8px;font-size:12px;opacity:.92}" +
    ".legend i{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:3px}" +
    "#map{height:calc(100% - 36px);width:100%}" +
    ".pin{background:transparent;border:0}" +
    ".pin span{display:flex;align-items:center;justify-content:center;width:22px;height:22px;border-radius:50%;color:#fff;font-size:11px;font-weight:700;border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.28)}" +
    "</style></head><body>" +
    '<div class="bar"><strong>' +
    escapeHtml(title) +
    "</strong><span class=\"legend\">" +
    legend +
    "</span><span style=\"margin-left:auto;opacity:.75\">滚轮缩放 · 拖动平移</span></div>" +
    '<div id="map"></div>' +
    '<script id="amapTaskData" type="application/json">' +
    json +
    "</script>" +
    '<script src="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js"></script>' +
    "<script>" +
    "var TASKS=JSON.parse(document.getElementById('amapTaskData').textContent||'[]');" +
    "var COLORS=['#2a9d8f','#c47a2c','#3d5a80','#9b4d7a','#c45c4a'];" +
    "function color(d){return COLORS[(Math.max(1,Number(d)||1)-1)%COLORS.length];}" +
    "function ll(p){return [Number(p[1]),Number(p[0])];}" +
    "var map=L.map('map',{zoomControl:true,scrollWheelZoom:true});" +
    "L.tileLayer('https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}',{subdomains:'1234',maxZoom:18,attribution:'高德地图'}).addTo(map);" +
    "var bounds=[];" +
    "TASKS.forEach(function(t){" +
    "if(t&&t.type==='route'&&t.start&&t.end){" +
    "L.polyline([ll(t.start),ll(t.end)],{color:color(t.day),weight:4,opacity:.85}).addTo(map);" +
    "bounds.push(ll(t.start),ll(t.end));}" +
    "});" +
    "var seen={};" +
    "TASKS.forEach(function(t){" +
    "if(!t||t.type!=='poi'||!t.lnglat||!t.label)return;" +
    "var k=Math.round(Number(t.lnglat[0])*1e4)+','+Math.round(Number(t.lnglat[1])*1e4);" +
    "if(seen[k])return;" +
    "seen[k]=1;" +
    "var n=t.label;" +
    "var latlng=ll(t.lnglat);bounds.push(latlng);" +
    "var icon=L.divIcon({className:'pin',html:'<span style=\"background:'+color(t.day)+'\">'+n+'</span>',iconSize:[22,22],iconAnchor:[11,11]});" +
    "L.marker(latlng,{icon:icon,zIndexOffset:1000-n}).addTo(map).bindPopup('<b>'+n+'. '+(t.text||'未命名')+'</b><br>第'+(t.day||1)+'天<br>'+(t.remark||'')).bindTooltip((t.text||'未命名'),{direction:'top',offset:[0,-10]});" +
    "});" +
    "if(bounds.length)map.fitBounds(bounds,{padding:[36,36],maxZoom:15});else map.setView([34.76,113.65],6);" +
    "</script></body></html>"
  );
}

function _checkPoiLabels() {
  const labels = stampPoiLabels([
    { type: "poi", lnglat: [116.3221, 39.8952], text: "北京西站" },
    { type: "poi", lnglat: [116.397, 39.918], text: "故宫" },
    { type: "poi", lnglat: [116.32214, 39.89518], text: "返回西站" },
  ])
    .filter((item) => item.type === "poi")
    .map((item) => item.label);
  if (labels.join(",") !== "1,2,1") throw new Error("poi labels " + labels.join(","));
  const gap = stampPoiLabels([
    { type: "poi", lnglat: [112.436284, 34.685924], text: "洛阳站" },
    { type: "poi", lnglat: [112.436284, 34.685924], text: "洛阳站附近" },
    { type: "poi", lnglat: [112.435579, 34.620466], text: "体育公园" },
  ])
    .filter((item) => item.type === "poi")
    .map((item) => item.label);
  if (gap.join(",") !== "1,1,2") throw new Error("poi gap " + gap.join(","));
}

if (typeof window === "undefined") _checkPoiLabels();
