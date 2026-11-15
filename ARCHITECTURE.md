# 架构

一次规划只有一条事件流：`planner/pipeline.py` 的 `stream_plan`。CLI（`app.py`）和 Web（`server.py` 的 `POST /api/plan`）都消费它。

```text
用户原话
  → 抽槽位（json_mode）；缺出发地 / 目的地 / 日期，或多日缺住宿地则 clarify 后结束本轮
  → 修订时合并槽位后统一复查；确认卡提交沿用确认后的槽位
  → 规则路由：要不要查票、要不要检索景点、要不要排路线、要不要出地图产物（planner/routing.py）
  → 需要票时为本次请求连接 12306 MCP（不在进程内共享；失败则 ticket 不可用）
  → iter_agents 根据规则用 asyncio 启动 map_agent / ticket_agent
  → 各子任务执行 bounded_loop：模型选白名单工具 → 执行 → 回传结果，受调用数与时间预算约束
  → map_agent 只交候选；编排层用高德复核坐标，可选 Tavily 取证，再纯排程
  → 同一份 days 写地图 HTML，并在闸门前替换行程表
  → stream_summary 无工具流式写六章节；首段和后续段等待均受成文截止时间限制
  → 用结构化事实替换票务、预算、行程表与景点建议，移除事实白名单以外的车次号与景点
  → 闸门：章节校验 + 车次号 + 景点名/耗时/顺序白名单 + 状态一致性，失败最多重试一次；仍失败或成文等待超时则确定性降级
  → 落盘 Markdown + 快照，yield final（含耗时、token 与 run_stats）
```

| 组件 | 做什么 | 不做什么 |
|------|--------|----------|
| `iter_agents` | 按规则用 `asyncio` 并发启动子任务；`FORCE_SERIAL=1` 先票务后地图 | 不通过主模型转发任务 |
| `bounded_loop` | `bind_tools` 后执行有界的模型/工具循环 | 不接受白名单以外的工具调用 |
| `map_agent` | 仅用 `amap_poi_search` 交候选 JSON | 不查 12306，不写最终 HTML，不碰 Tavily 密钥 |
| `ticket_agent` | 仅调用查询所需的 MCP 工具，原始响应另存并提取票务事实 | 模型总结不直接作为车次事实 |
| 编排层 | 复核坐标、取证、排程；同一份 `days` 生成地图与行程表 | 不把未知耗时或开放规则当作已核实 |
| `stream_summary` | 无工具成文，校验失败最多重试一次 | 不能继续检索或改文件 |
| 路由 | `initial` / `revision` 原话、确认表单改动与持久化跨城意图共同决定是否查票 | 不用市内 `transport_mode` 或 LLM 决定是否调用工具 |

票务六态为 `ok` / `skipped` / `unavailable` / `missed` / `timeout` / `reused`。`reused` 标注上次查询时间，余票未刷新，质量检查不把它标为当前已核实。`final` 表示流程结束；可靠程度另由 `quality.status` 的 `complete` / `partial` / `degraded` 表达。

地图相关的判断拆成三层（`planner/routing.py` 的 `map_scope()`）：`need_poi_search`（要不要检索景点）、`need_route`（行程里要不要排路线）、`need_map_artifact`（要不要写地图 HTML）。三者分开是必要的——把它们混成一个 `need_map` 布尔会出现「用户说不要找景点，系统仍然检索并生成具体景点」；反过来「不要地图」只该否决产物，结构化行程与行程表照旧生成。行程实际来源记在 `route_source` 上：`built`（本轮真排程）、`reused`（完整沿用父方案）、`none`（没有可信结构化行程，正文只能如实说缺）。


澄清是结束当前请求后带确认槽位重新请求，不是 LangGraph 的 `interrupt` 或检查点恢复。当前主路径不调用 `create_deep_agent`，也不挂载文件系统或 shell；工具面分离不等同于操作系统级沙箱。

## 查询事实 / 适用事实 / 规划依据 / 实际出行安排

票务这条链上有四层，混在一起就会出现「查到了车次」被当成「这趟能用」：

| 层 | 数据 | 判据 |
|----|------|------|
| 查询事实 | `parse_ticket_payload` 从工具原始响应解析出的行 | 只认 `get-tickets` 这类车次结果工具；站点查询不算车次 |
| 适用事实 | `_annotate` 打上 `direction` / `station_match` / `applied` / `reason` 之后的同一批行 | 区间、方向、日期任一不符就是 `applied=False`，并保留 `reason` |
| 规划依据 | `choose_plan_basis` 选出的那一趟（`plan_basis`） | 用户原话点名的优先；否则去程取当日最早抵达且有可售座席的一趟 |
| 实际出行安排 | `itinerary.days` 里的时刻与路段 | 由排程按规划依据推出来，仍需按开放规则与耗时逐项核实 |

`quality.checks.ticket` 只看**适用事实**：查到了 20 行但一行都不适用，仍然是 `unverified`，并给出 `ticket_not_applicable` 而不是「票务响应无法识别」。票务工具里还有未启用解析的工具（如 `get-interline-tickets`）时只产 `notes`，不升级成 warning。

## 正文里的铁路状态四态

需求摘要的「铁路」一行不能只用「查票 / 不查票」二分，否则自驾与同城一日游也会被写上「返程尚未选定」。它由三个正交判断组合出来：

| 判断 | 取值 | 来源 |
|------|------|------|
| 行程是否依赖铁路 | 是 / 否 | `rail_required`（自驾、同城、明确说不坐火车时为「否」） |
| 本轮是否真的执行了查询 | 是（`ok`）/ 否（`skipped` / `unavailable` / `missed` / `timeout`）/ 复用（`reused`） | `resolve_ticket_state` 的票务状态 |
| 是否需要返程铁路 | 是 / 否 | `rail_required` 的 `inbound` 标记（单日或当天往返时为「否」） |

组合规则：

- 不依赖铁路 → 「本次未查票（无需铁路）」，**不出现**「返程尚未选定」。
- 依赖铁路且本轮查过/复用 → 「本次需要查票，结果见『车票建议』一节」；若返程需要但 `plan_basis.inbound` 仍为空，另起一行说明「返程尚未选定」并列出候选车次，写成候选而不是已锁定。
- 依赖铁路但本轮未查（`skipped` / `unavailable` / `timeout`）→ 如实写「未查票」，不写成「已查、无结果」。
- 复用（`reused`）→ 写「沿用上次查询」，并带上原查询时间，不写成「本次查询」。

这四态只影响正文措辞，不改 `quality` 判断：`checks.ticket` 在复用与未查时仍分别是 `unverified` 与 `not_required`。

## 住宿三态与精度判定

住宿不是一个字符串，而是三种状态 + 一个精度：

- `kind=exact`：用户给了具体酒店，且高德返回的名称能对上（`_identity_matches`）。可以当出发与返回锚点。
- `kind=area`：只给到「上海站附近」这类区域。锚点取该区域的 POI，但**不宣称已确认酒店**，`pending_confirmations` 里保留一项等用户选「填具体酒店 / 按该区域参考规划」。
- `kind=unresolved`：给了具体酒店但高德匹配不上。按目的地中心暂代，并如实说明。

住宿 POI 不进入景点候选池；候选自带的 `must_visit=true` 不被采纳，只有用户原话点名的才算必去项。未定位按 `kind` 分成 `sight` / `must_visit` / `lodging` / `station`：只有前两类进开放规则分母、也只有 `must_visit` 会让排程判 `conflict`。

## 首末日窗口、跨日与假设缓冲

抵离时刻来自车次事实（`depart` / `arrive` / `depart_date` / `arrive_date`）。`rail_window` 把它换算成每天可用的窗口：

- 首日 `earliest_start = 抵达时刻 + 接驳估算`，末日 `latest_end = 发车时刻 - 进站估算 - 接驳估算`。
- 缓冲（`RAIL_TRANSFER_MIN=40`、`RAIL_BOARD_MIN=30`）是**规划假设**，随文案一起写明，不写成已核实事实。
- 车次抵达日已晚于首日 → 首日 `unavailable`，保留为「主要用于抵离」的一天，不静默吞掉、也不算冲突。
- 时刻未知时窗口状态是 `unknown` 而不是退回 09:00–18:00 假装知道；当天所有时刻标 `unverified`。
- 没有规划依据（没查票 / 不适用）时是 `not_constrained`，才允许用默认窗口。

## 确认提交与路由重判

补充与确认卡（原生 `<dialog>`）提交时走 `mode="confirm"`：

1. `base = snapshot.slots`，`merged = merge_slots(base, 本轮 slots)`，`changed = diff_fields(base, merged)`。`slots.transport_mode` 仅是市内游览方式；跨城 `intercity_mode` 与 `return_required` 存在 `request_context`。
2. `merge_request_context` 合并确认状态：**先按 `changed` 失效旧的确认，再应用本轮显式值**（顺序反了会把用户刚点的「按该区域参考」又清掉）。
3. `resolve_revision_kind(changed, context_changed, instruction)` 决定走哪条。判定顺序**固定**，顺序本身就是决策的一部分：

   ```text
   ① 原话明确要求刷新余票 / 重新查票（即使槽位没变、即使同时改了预算）
        → ticket_and_map
   ② 日期 / 出发地 / 目的地 / 天数，或跨城方式 / 往返意图变化
        → ticket_and_map
   ③ 原话明确点名车次或指定返程时段
        → map_only（在已有适用事实里选车，只重算依据、窗口与排程）
   ④ 原话明确要求重排行程 / 景点顺序
        → map_only
   ⑤ 原话明确要求重新定位住宿区域 / 参考地点
        → map_only（纯原话动作、无槽位变化，也要重跑住宿定位与往返接驳）
   ⑥ 景点 / 住宿 / 路线条件变化（含住宿确认范围）
        → map_only
   ⑦ 只有预算 / 人数 / 预算口径变化
        → budget_only（只重算预算与成文，不查票不查图）
   ⑧ 没有任何结构化或上下文变化、原话也没点明动作
        → summary_only（完整复用上一版）
   ```

   **选车不能覆盖天数变化。** 天数一改，返程日期与票务区间跟着变，父方案那批适用事实已经不成立；若因为原话里点了车次就判成 `map_only`，就会在旧事实里挑车、再用旧窗口排新天数，产出「日期变了、票还是原来那张」的方案。所以票务字段与天数的判定排在选车之前，命中即升到 `ticket_and_map`。同理，`①` 必须排在 `③` 之前：「返程改为下午，顺便重新查询余票」既命中选车也命中刷新，用户要的是新票，不能只重算排程。

   结构化变化优先于原话措辞：既没结构化变化、原话也没点明动作时就是 `summary_only`；`classify_revision` 的默认 `map_only` 与普通关键词命中（例如「自驾」命中交通词）都不能当作升级依据。
4. `summary_only` 与 `budget_only` 都走完整复用：票务事实、行程、地图文件、定位约束、候选与 `itinerary_needs_recheck` 全部沿用父方案，只重出正文。预算分支不能只恢复票务数据——那会让行程变 null、地图不复制、待复核标记被错误清零。
5. 再跑一次 `must_pause`：关键槽位缺失或住宿描述完全没有才停下追问；只到区域、人数未知、预算口径未知都只产出带 `quality.pending` 的草稿。

### 票务复用校验

能不能沿用旧票，由 `_ticket_fields_match` 判定。它比较 `date` / `origin` / `destination`、持久化的跨城方式与往返意图，以及 **`days`**（天数）和**派生的实际返程日期**；市内 `transport_mode` 不参与。任一字段不同即判定为不复用，走 `ticket_and_map` 重查。

这一条与第 ③ 步的「点车即 `map_only`」是互补的：选车只在**票务字段与天数都没变**时才允许，否则 `map_only` 会在失效的事实里挑车。

本轮原话里说出的人数（「改成三个人」）也要写回 `request_context.party_size`，否则本轮预算按 3 人算、快照里还留着 2 人，下一轮又会退回旧口径。

`mode="revise"` 只带原话与 `plan_id`，不再回传整份旧槽位——整份回传会盖住本轮修改意图。确认状态落在快照 `request_context` 里，隔轮修订仍能复用。

## 修订复用的执行顺序

复用与重建共用同一条顺序，顺序错了会出现「依据是新的、窗口还是旧的」：

```text
读取旧快照并归一化（load_snapshot_v4）
  → 合并本轮修改（merge_slots / merge_request_context）
  → 按变化失效不适用的依据（日期、区间、跨城方式或往返意图变了就两个方向一起失效）
  → 继承父方案票务事实，按当前 expect_date / return_date 重新验证（revalidate_facts）
  → 应用本轮选车（原话点名车次优先，其次本轮时段偏好，再次父方案仍适用的选择）
  → 计算最终 rail_window
  → 排程
  → 同一份结果同步写入 request_context / 顶层 / constraints / 快照
```

**继承的事实先重验，再进依据。** 快照里的票务事实带着 `direction` / `applied` / `reason`，但那是**按父方案的日期与区间**算出来的。复用链不能直接采信父方案的 `applied=True`：`revalidate_facts` 会按本轮的 `expect_date` / `return_date` 重新判定方向（去程 / 返程）、是否适用及原因，重算完再交给 `choose_plan_basis` 收敛成规划依据。顺序不能颠倒——先选车再重验，等于用旧结论挑车，日期一变就会留下一趟根本不成立的"依据"。

适用范围：只有本轮明确没有重查、行程条件（日期、区间、跨城方式、往返意图）确实没变时，才能沿用旧依据。重新查询失败、空结果、无适用事实或无票时旧依据一律不作为本轮结论；用户曾点名的车保留为选择意图，但当前 `plan_basis` 置空，不能静默换车。可售只认明确“有票”或正的剩余张数。

`rail_window` 是**派生结果**，不是可以继承的结论：它由「规划依据的抵离时刻 + 接驳与进站缓冲 + 天数」算出来。依据一变（换了车次、改了日期、返程从待定变成选定），窗口就必须重算，不能把上一版的窗口原样复制过去。同一份 `plan_basis` / `rail_window` 要同时写进 `request_context.plan_basis`、顶层 `plan_basis`、`constraints.plan_basis` 和快照，四处不一致时以本轮重算的为准；旧快照缺顶层 `plan_basis` 时只从 `constraints` 回填一次作为兼容读取，不再无条件回填。

### 复用与重建共用的告警位置

`route_unverified` 与 `search_unavailable` 这两类 warning 发在**复用与重建两条路径共同经过的位置**（合并之后、成文之前），而不是各自分支里。这样只要结论里带上了未核实的交通或开放规则，就一定有对应告警，不会因为走了 `summary_only` / `budget_only` 复用分支而整段消失。

复用带回来的失败原因保留原口径，文案是「原路线查询因预算限制未完成，本次沿用该结果」——说明这是上次的结论，不是本轮的查询结果。本轮重新查询成功时必须清空继承来的旧 `route_error` / `search_error`，否则页面会一直挂着一条已经不再成立的失败。

### 分支继承矩阵

| 分支 | 继承什么 | 不继承什么 |
|------|----------|------------|
| `summary_only` | 全部：票务事实与原文、行程、地图文件、约束、候选、`unlocated`、路由状态、失败原因、`plan_basis` / `rail_window`、待复核标记 | 父方案的 `quality` 结论（`pending` / `issues` / `sources` 一律按本轮重算） |
| `budget_only` | 同上（预算变了不代表票、行程、地图也变了） | 同上 |
| `map_only` | 只继承票务四字段（`ticket_facts` / `ticket_raw` / `ticket_reason` / `ticket_queried_at`）与**未变方向**的 `plan_basis` | 路线状态、失败原因、未定位项、`rail_window` 一律用本轮重建结果覆盖 |
| `ticket_and_map` | 不继承任何旧依据 | 旧票、旧行程、旧地图、旧约束一律重算 |

`map_only` 的依据要按变化方向逐个失效，不是整份照抄：票务字段（日期 / 区间 / 交通方式）变了就两个方向一起失效；只有天数变了则保留去程、清空返程（天数一变返程日期就变）。失效的方向记进 `plan_basis.<direction>` 并标为未知，排程据此重算窗口，而不是拿继承来的 `rail_window` 配新路线。

复用分支恢复字段时，缺字段要落进 `inherited_unknown_fields`：这样「父方案本来就没有这个字段」和「我们漏读了」能区分开，缺的字段按未知处理，不拿默认值冒充已核实。

父方案的 `quality` 结论一律不继承：`quality` 描述的是「那一版方案的核实程度」，本轮重算出来的方案必须重新判 `checks` 与 `status`，否则会拿旧方案的 `complete` 掩盖本轮的未知项。

### 质量明细的归属与聚合压制

排程类未知不再只有一条笼统结论，`schedule_details` 会把它们拆成明细，每条明细声明自己覆盖哪个类别：

| 明细 code | 覆盖类别（`_DETAIL_COVERS`） |
|-----------|------------------------------|
| `travel_leg_unknown` | `travel_time` |
| `opening_missing` | `opening_rules` |
| `rail_window_unknown` | `schedule` |
| `candidate_shortage` / `schedule_sparse` | `schedule` |

`collect_issues` 用 `covered` 集合记录已被明细解释的类别，只有 `_AGGREGATE_CATEGORY` 里对应的聚合项（`travel_unverified` / `opening_unverified` / `schedule_unverified`）落在 `covered` 里才省略；没被覆盖到的类别照旧输出聚合项。明细只压自己解释得了的那一类，不能因为「有交通明细」就把开放规则的聚合结论一起吞掉。

排程先按当天窗口、节奏和候选实际游览分钟计算 `days[].density`，再决定是否需要补充候选；已知短抵离窗口会缩放活动负荷参考目标，窗口未知只保留活动分钟而不按完整日补点，超过上限标为偏紧但不替代既有时间冲突。密度只说明活动负荷，不替代路线、开放和抵离窗口的可行性判断。`candidate_supply` 区分检索目标、已定位可用候选、最终日期缺口及每个缺口日可实际放入的候选；只有这些候选仍无法覆盖具体日期时才产生 `candidate_shortage`。补充至多执行一轮：按缺口日共享预算检索、定位/取证、固定原分配插入新增候选、查询新增必要路段；若最终放不下优先撤回新增候选，随后只用已有结果收敛，不循环追加请求。

抵离时刻未知的明细由 `_rail_window_details` 生成，按 `rail_window.outbound` / `inbound` 的 `state == "unknown"` 分方向给：已选车次缺抵达/出发时刻、去程未定、返程未定（且有候选时另由 `return_basis_pending` 承担「请你选」的语义）。`rail_window` 未传时退回按天 `window.state` 兜底，避免旧调用方丢明细。

票务告警的输入是 `ticket_state`（`reason` / `queried_at` / `facts_count` / `applicable_count` / `diagnostics` / `parent_reason` / `parent_class` / `reuse_source` / `parent_diagnostics`）：

- `reason` 必须是 `resolve_ticket_state` 的语义值（`ok` / `reused` / `skipped` / …），不是 `checks.ticket`——前者决定「沿用」与「未查」的区分，后者只是校验结果。
- `parent_reason` 是父方案当时的 `ticket_reason` 原值，`parent_class` 是它的聚合分类（`ok` / `skipped` / `failed` / `unknown`），`reuse_source` 是面向正文与前端的那一层（`parent_ok` / `parent_skipped` / `parent_failed` / `parent_reused` / `parent_unknown`）。三者并存而不是互相顶替：只有「上次本来就没查出结果」才说「上次查询失败 / 无结果 / 超时，本轮沿用该结果」；上次查成功只是没刷新的，说「余票未刷新」；上次没查票的，说「本次无需铁路」。只留一个笼统值会把失败与成功混成一句。
- 复用状态由 `planner/pipeline.py` 的 `_reuse_state()` 统一构造，三处复用分支共用同一份取值，避免同一请求里正文、质量结论、SSE step 三处说法不一致。`ticket_reuse` 三元组随快照落盘：父方案自己也可能是复用来的（它的 `ticket_reason` 已被压成 `reused`），只剩这层还留着最初那次查询的真实结论；丢了它，孙代就会把「最初查失败」说成「沿用上次结果」。旧快照没有该字段时按 `parent_unknown` 兼容，不凭空断言成功。


### 快照写入与旧快照兼容

快照（`planner/summary.py` 的 `save_snapshot`，`schema_version=4`）除原有的槽位、票务态、行程、约束、候选外，还写入：

| 字段 | 内容 |
|------|------|
| `unlocated` | 未定位项，带 `kind`（`sight` / `must_visit` / `lodging` / `station`）与 `reason` |
| `ticket_diagnostics` | 车次解析的诊断记录，用于还原「为什么没生成事实」 |
| `ticket_notes` | 未启用解析的工具说明，只作提示，不升级成告警 |
| `lodging_state` | 住宿口径：`scope` / `kind` / `precision` / `poi_id` / `text` |
| `return_basis` | 返程依据：候选车次列表与当前是否已选定 |
| `request_context.intercity_mode` / `return_required` | 持久化跨城方式与是否往返；确认表单的实际改动覆盖回传旧原话 |
| `request_context.cleared` | 已撤销确认项；阻止历史原话、继承偏好和旧上下文自动回填，直到用户重新明确赋值 |
| `request_context.selected_train_intent` | 用户点名的去程/返程车次意图；即使刷新后当前依据降级为空也保留，不把它当可售事实 |
| `ticket_reuse` | `parent_reason` / `parent_class` / `reuse_source`，让下一轮复用还能说出父方案当年的真实结论 |

旧快照（`schema_version < 4` 或字段缺失）走兼容读取：缺的字段标为**未知**，不伪造默认值，也不假装成已核实。`load_snapshot_v4` 从旧的 `ticket_raw` 重新解析抵离时刻（能补就补，补不出来保持 `None`），适用性标为 `None`，并打 `legacy` 与 `itinerary_needs_recheck=True`；沿用这种行程时耗时与排程一律 `unverified`。

## 成文闸门与结构化事实

闸门分两层，缺一层都会留下编造空间：

| 层 | 拦住什么 | 实现 |
|----|----------|------|
| 章节与车次号 | 六章节缺失、白名单外的车次号 | `summary_gate_issues` + `assert_no_fabricated_trains` |
| 景点与耗时 | 正文出现行程外的景点、与 `days[].items` 不一致的先后顺序、未核实的游玩耗时、把 `reference` 写成「已实测」、无结构化行程却断言具体景点 | `planner/summary.py` 的 `poi_gate_issues`，白名单来自 `itinerary_whitelist` |

白名单按名称分池：可游览名来自 `days[].items[].name`（`role=visit`），住宿名与车站名单独成池。混成一个集合时，站名或酒店名会被当成景点，反之行程表里的合法站名会被误判成越界。耗时只与当天 `days[].legs` 边（`duration_seconds`）或 `items` 的 `start`–`end` 时长比对——同一个「12 分钟」可能对应不同路段，按数字全局匹配会误报；当前路线接口不带日期与时段，边只到 `reference` 档，正文不得把它写成已实测。

闸门只做最后检查，事实替换不交给它：有结构化行程时「景点建议」整节由 `sights_suggestion_markdown` 用 `days` 确定性生成，「行程表」「车票建议」「预算」同样由结构化数据替换。模型写的景点名、耗时、顺序随之作废，闸门再兜一遍漏网的。

已知漏检（诚实记录，不假装覆盖）：名称是子串+逐行匹配，通俗简称、候选内子景点、错别字会漏；含「未能定位/未排入/备选/候选/待检索」的行整行放行，模型换个说法可绕过；耗时只解析整数分钟（「约半小时」「1 小时 20 分」不解析）；状态检查是词表匹配；中文月日只在「第N天…M月D日」16 字窗口内识别。

## 出行日期未知

底层排程允许 `date=None` 的 days（`schedule_status=date_unknown`）：`_dates()` 返回 `None` 而不是伪造首日；`day_window` 给 `state=unknown`；开放规则、日期适用性、候选缺口与密度目标一律返回未知，不按假日期算出看似确定的数字。`itinerary_markdown` 把缺失日期写成「日期待定」。

规划主流程仍要求关键槽位齐全，缺日期先澄清，不会带着未知日期直接出正式方案；结构化行程存在但日期未知时，正文与地图都只能标未核实。

## 请求生命周期与取消

```text
客户端断开 / SSE 总超时 / 预算耗尽
  → cancel_event 置位，停止新的外部请求与模型调用
  → 子任务 cancel，并在 PLAN_CLEANUP_SECONDS 内等待收尾（drain）
  → 清理窗口内结束：正常关闭本次请求的 MCP 连接
  → 清理窗口内没结束：记 subagent_leak + 任务名；关 MCP 时另记 mcp_closed_after_timeout；产生 warning
  → 释放 inflight 名额，关闭事件流
```

`drain()` 用 `asyncio.shield` 包住等待任务，防止二次取消跳过清理。工具调用本身也带超时（`RecordingTool.ainvoke` 的 `asyncio.wait_for`）：只靠 `Task.cancel()` 取消到的是包装层，底下的网络等待仍会挂着。

「调用了 `close_ticket_agent()`」不等于「没有资源泄漏」：`subagent_leak` 与 `mcp_closed_after_timeout` 分开记，就是为了不把「已经尽力关」读成「已经关干净」。

SSE 外层总超时由 `SSE_TOTAL_TIMEOUT_SECONDS` 控制（默认 `PLAN_BUDGET_SECONDS + PLAN_CLOSEOUT_SECONDS + 30`）。超时发 `error(code=sse_timeout)` 并结束流；`error` 是终态，之后不会再发 `final`。

## 请求级运行统计

`RunStats`（`planner/run_stats.py`）是请求级上下文（ContextVar，沿用 `planner/usage.py` 的模式），各层只写自己拥有的那一步：`ticket_cache` 写连接、`RecordingTool` 写单次查询、`map_fallback` 写 POI 与路线、`travel_search` 写取证、`subruns` 写子智能体调度、`server.py` 写 SSE 层。最后统一序列化进 `final.timings.run_stats` 与规划记录的 `usage` 列，不加新列。

注意 `watch_aiter` 用 `asyncio.create_task(agen.__anext__())` 逐帧驱动，`create_task` 会复制当前 context：只在生成器首帧里 `begin_run_stats()` 的话，第二帧与深层子任务读到的都是 `None`。因此 `server.py` 自己持有 `request_stats`，在 `event_source()` 生成器里 `begin_run_stats(request_stats)` 并显式 `run_stats=request_stats` 传给 `stream_plan`；总超时与客户端断开由 server 直接 `request_stats.note("sse", ...)`，不依赖 ContextVar 传播。

九档状态（`attempted` / `succeeded` / `failed` / `timed_out` / `cancelled` / `rate_limited` / `blocked_by_budget` / `dependency_missing` / `not_needed`）必须分开：`tool_attempts=0` 本身分不清「没必要调用」「被预算拒绝」「依赖缺失」「用户取消」「路由跳过」。统计只存状态与计数，不写 query、工具原始响应或模型全文。

决策记录：

- [ADR-1 为什么 summary 不是 agent](docs/adr/0001-summary-is-not-an-agent.md)
- [ADR-2 为什么路由用规则](docs/adr/0002-routing-is-rules.md)
- [ADR-3 历史 shell 边界](docs/adr/0003-shell-boundary.md)
- [ADR-4 子智能体隔离工具面](docs/adr/0004-subagent-is-tool-surface.md)
- [ADR-5 方案依据与对象角色](docs/adr/0005-plan-basis-and-object-roles.md)

`amap-lbs-skill/` 保留高德脚本，当前规划由 HTTP POI 工具与编排层完成，不走 `LocalShellBackend`。方案与快照在 `workspace/results/`。

## 受控产物与会话

`/results/旅游规划-…-{uuid}.md` 与 `/results/maps/{uuid}.html` 保留历史 URL 形状，但不再静态挂载整个结果目录：路由只接受固定文件名和 UUID，并按 API 认证读取 Markdown 或地图 HTML，响应为私有、禁止共享缓存。快照、SQLite 数据库和 SQLite 辅助文件无 HTTP 路由。

配置 `PLAN_API_KEY` 后，`POST /api/session` 以口令创建随机、不透明的 HttpOnly `SameSite=Strict` Cookie；服务端单进程内存中只保留令牌哈希、到期时间和当前口令哈希。默认有效期由 `PLAN_SESSION_TTL_SECONDS` 控制（8 小时），重启或口令轮换立即失效。带 `X-API-Key` 的请求优先校验该头，错误头返回 401；Cookie 的写请求校验允许 `Origin`。公网 / 反向代理必须用 HTTPS 并设 `PLAN_COOKIE_SECURE=1`。未配置口令时保留本地免认证。

离线回归覆盖修订优先级与重新澄清、成文卡流超时、黄金集失败留存与期望检查、回放成功或异常后的环境恢复。它们验证控制层契约，不能替代真实服务的当前延迟、成本与效果基线；`EVALS.md` 的旧流水线数字只作历史。

真实黄金集以 `evals/run_golden.py --as-of YYYY-MM-DD` 运行。运行器使用 `Asia/Shanghai` 和固定的“严格下一个周六”算法，在父/子轮间传递同一日期上下文；每条结果保留 fixture SHA、基准日、旅行日、父案例与父方案 ID。未满足密钥、授权或费用前置条件的案例为 `blocked`，不计为通过；跨城首场失败后，未运行案例为 `blocked_by_previous_failure`。
