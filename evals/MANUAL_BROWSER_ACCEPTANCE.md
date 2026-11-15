# 浏览器人工验收

本清单不调用真实模型、高德、MCP 或 Tavily。使用 `evals/fixtures/browser-map.html` 作为固定地图内容，并在记录中绑定实际 commit SHA。

## 前置条件

1. 用单进程启动本地服务：`python -m uvicorn server:app --host 127.0.0.1 --port 8000`。
2. 记录浏览器与操作系统版本；不要使用无头浏览器替代人工结论。
3. 为每个分支保留以下字段：`browser`、`browser_version`、`os`、`server`、`git_sha`、`plan_id`、`fixture_path`、`branch`、`authentication`、`result`、`screenshot`、`console_errors`、`network_errors`。

## 两个 iframe 分支

- `srcdoc`：用固定夹具的地图 URL 让 `loadMapMeta()` 成功读取 HTML，确认 iframe 显示“本地地图验收夹具”。
- `direct-url`：使用同一夹具 URL，但让测试元数据不提供 `mapViewerHtml`，确认 iframe 仍通过 `/results/maps/{uuid}.html` 显示夹具内容。
- 两个分支均检查 iframe 的 `sandbox` 不包含 `allow-same-origin`；在 iframe 控制台访问 `parent.document`、`parent.localStorage` 和 `parent.document.cookie` 必须失败或不可读。
- 点击夹具中的“高德在线地图”，确认 `_blank` 页面能打开。网络限速后先发起旧请求、再发起新请求，确认旧请求不覆盖新请求的正文、地图、告警或 `plan_id`。

## 认证矩阵

| PLAN_API_KEY | Cookie | X-API-Key | 预期 |
|---|---|---|---|
| 未配置 | 无 | 无 | 地图 URL 返回 200 |
| 已配置 | 无 | 无 | 地图 URL 返回 401 |
| 已配置 | 有效 | 无 | 同源地图 URL 返回 200 |
| 已配置 | 有效 | 错误 | 返回 401，错误请求头不能被 Cookie 掩盖 |

会话通过 `/api/session` 获取 HttpOnly Cookie；Cookie 写请求必须携带受信任 `Origin`。本清单只覆盖记录中的浏览器版本，不证明移动端、跨浏览器、HTTPS、反向代理或未来浏览器版本行为。
