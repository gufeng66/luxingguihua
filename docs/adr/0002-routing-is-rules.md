# ADR-2：路由用规则，不用 LLM

## 状态

接受

## 背景

「要不要连 12306」「要不要跑 map_agent」会花钱，也会触发有副作用的外部调用。用模型决定这两件事，单测无法在不付费的情况下断言同一句话永远走同一条路径。

## 决策

`planner/routing.py` 用槽位和关键词决定跳过原因（同城、自驾、飞机、用户明确不要地图、修订类型）。`resolve_ticket_state` / `resolve_map_state` 在调度结束后给出 `ok` / `skipped` / `unavailable` / `missed` / `timeout` / `reused`（地图另有 `incomplete`）。这些函数不发起网络请求。

槽位抽取仍然用 LLM，而且只用 `json_mode` 一次成型。抽取失败时 pipeline 退回按原话规划，不在路由层再问一次模型。

## 后果

新说法要加关键词才能识别，不会自动泛化。换来的是 `evals/test_routing.py` 可以离线锁定每条用例的查票与出图决定。
