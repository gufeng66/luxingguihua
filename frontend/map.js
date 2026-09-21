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
