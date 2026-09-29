# 评测口径（与 `evals/test_routing.py` 顶部 `CASES` 同构）

改任一条须两边同步。不要写 md→pytest 解析器。

## `resolve_ticket_state` 优先级（禁止改序）

1. `has_ticket_result` → `ok`
2. `not need_ticket` → `skipped`（即使 MCP 挂了也不得写成服务不可用）
3. `not ticket_mounted` → `unavailable`
4. `timed_out` → `timeout`
5. 否则 → `missed`



## 路由 CASES

| id | 输入要点 | need_ticket | 车票建议关键词 |
|----|----------|-------------|----------------|
| cross_city_hsr | 新乡坐高铁去郑州 | True | 可含车次（ok 时） |
| xinxiang_drive_luoyang | 新乡租电车自驾洛阳 | False / no_rail_intent | 未查票（无需铁路） |
| intra_city | 杭州自驾不坐火车 | False / no_rail_intent | 未查票（无需铁路） |
| explicit_hsr | 明确坐高铁跨城 | True | — |
| plane_no_rail | 坐飞机 | False / no_rail_intent | 未查票（无需铁路） |
| no_plane_needs_rail | 不坐飞机 | True | — |

## 边界

- 杭州 vs 杭州市西湖区：同城
- 新乡 vs 新乡县：不同城（避免误合并）
- 跨城但 MCP 失败：`unavailable`，关键词「不可用」
- 已挂载只调 map：`missed` + warning `ticket_not_dispatched`，关键词「未调度」
- dispatch 超时无 ticket chunk：`timeout`，关键词「超时」
- 需要找景点时调度不设超时，等 map 写完或客户端断开；不得因 `DISPATCH_TIMEOUT` 标成跳过
- 用户说「不要地图」或「不要找景点」：跳过 map，不生成 HTML
- 修订 map_only：不调用 `extract_slots` / `get_ticket_agent`；票务态 `reused` 或 `skipped`
- `plan_id=../../.env`：error，不读盘

## 修订分类

是否改变 `(date, origin, destination, 出行方式)`？是 → `ticket_and_map`；行程内容 → `map_only`；只改措辞 → `summary_only`。旧票 `ok` 且指令含交通词 → `ticket_and_map`。
