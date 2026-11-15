# 项目协作规范

本文用于维护「旅行规划多智能体」项目。修改前先核对当前源码；发现文档与实现不一致时，说明差异并修正相关说明，不能将历史实现当作当前能力。

## 1. 沟通与操作约束

- 默认使用中文回复、撰写说明；用户明确要求其他语言时再切换。
- 动机或目标不清晰时先讨论；目标清晰但方案不合适时，直接说明原因并建议更好的办法。
- 遇到问题先定位根因。修改应能解释「为什么需要」，优先沿用现有模块，避免为当前项目不可能出现的情况增加复杂设计。
- 下载或安装任何软件、工具、依赖包、可执行文件前，必须先说明用途并获得用户明确确认。除 skill 外，安装位置必须在 `E:\codex\_xz`；README 中的安装示例不代表已经获得授权。
- 每次修改文件前重新读取目标文件的最新内容；新建文件前确认是否已存在。保留用户已有的未提交改动，不擅自覆盖、回退或清理。
- 认为目录为空时，必须再使用 `dir` 验证。
- 删除重要或敏感文件前必须征得用户同意。任务结束时清理本次任务产生的垃圾、临时与测试产物，不能把项目测试源码或用户原有文件当作垃圾删除。
- PowerShell 命令必须显式使用以下形式，并将 `Get-Content`、`Set-Content` 等文件读写操作的编码设为 UTF-8：

  ```text
  powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; <命令>"
  ```

## 2. 项目入口与职责

Python 3.11+；后端为 FastAPI，前端为本地静态 Vue 3 页面。Web 与 CLI 共用规划核心，当前主路径不需要 Node.js 或前端构建步骤。

| 位置 | 维护职责 |
| --- | --- |
| `planner_service.py` | CLI、Web、测试的公共导入入口；保留现有导出和测试替换所依赖的符号 |
| `planner/pipeline.py` | `stream_plan` 异步事件流，串联槽位、澄清、路由、子任务、成文与落盘 |
| `slots.py`、`planner/llm.py` | 槽位模型、校验、日期换算与模型抽取 |
| `planner/routing.py` | 纯规则路由、修订分类与槽位合并 |
| `planner/subruns.py`、`planner/budget.py` | asyncio 调度、有界模型/工具循环、调用次数和时间预算 |
| `planner/map_agent.py`、`planner/map_fallback.py` | 景点候选、坐标复核、高德调用和地图产物 |
| `planner/travel_search.py`、`planner/sources.json` | 开放规则取证、来源白名单与证据解析 |
| `planner/route_plan.py` | 路线与行程排程、结构化行程表 |
| `planner/ticket_agent.py`、`planner/ticket_cache.py`、`planner/ticket_facts.py` | 票务查询工具、请求级 MCP 生命周期、原始响应和票务事实 |
| `planner/summary.py`、`planner/quality.py`、`planner/usage.py` | 无工具成文、校验与降级、质量状态、token 和成本统计 |
| `planner/paths.py`、`planner/plans_store.py` | 路径与环境初始化、SQLite 规划记录 |
| `server.py`、`app.py` | HTTP/SSE 与 CLI 适配；规划业务规则放在核心模块 |
| `frontend/` | 页面、交互、SSE 消费与示例；第三方脚本在 `frontend/vendor/` |
| `evals/`、`docs/adr/` | 离线回归、真实调用评测与架构决策 |

`planner/dispatch.py`、`planner/backends.py` 和 `amap-lbs-skill/` 保留了历史实现。修改前确认是否被当前路径调用，不能因依赖或旧符号仍存在就认定主流程正在使用它。

## 3. 编排与权限边界

- 外层工作流以 `stream_plan` 为准。当前澄清是发送 `clarify` 后结束本轮，下一次请求带确认后的 `slots` 继续；不能描述为 LangGraph `interrupt()`、检查点恢复或断点续跑。
- 是否查票、是否出地图、修订走哪个分支，由 `planner/routing.py` 的槽位与关键词规则决定。路由函数保持纯函数，不在其中联网或再次询问模型。
- `iter_agents` 按规则用 asyncio 启动 map/ticket 子任务；`FORCE_SERIAL=1` 必须由代码保证先票务后地图。
- 地图子任务只暴露 `amap_poi_search`；坐标复核、开放规则取证、排程和最终地图 HTML 由编排层完成。
- 票务子任务只挂载 `TICKET_QUERY_TOOLS` 白名单中的查询工具。MCP 连接按请求创建和关闭，不因为文件名含 `cache` 就恢复进程共享连接。
- 成文阶段保持无工具调用。当前主路径不使用 `create_deep_agent`，不向地图、票务或成文模型暴露文件系统、shell 和任意执行工具。
- 工具白名单属于应用权限控制，不能宣传为操作系统级沙箱。

## 4. 槽位、修订与事实约束

- `origin`、`destination`、`date` 是关键槽位；多日行程还要求 `lodging`。缺失时先澄清，软字段按 `slots.py` 的既有默认策略处理。
- 澄清判定以 `slots.py` 的 `input_state` / `must_pause` 为准：只到区域、人数未知、预算口径未知属于「待确认」，产出带 `quality.pending` 的草稿，不当作硬阻塞；住区描述完全缺失才停下来追问。
- 修订优先使用本轮明确意图，其次使用合并后的交通槽位，最后参考历史原话。修订合并后必须重新检查必需字段，包括从单日改为多日后补住宿地。
- 修订分支按「实际变化 + 本轮明确动作」判定：原话明确要求重新查票/刷新余票才升到查票，明确要求重排顺序才重排；既无结构化变化又没点明动作时是 `summary_only`。`summary_only` 与 `budget_only` 都走完整复用（票务、行程、地图、约束、候选、待复核标记），只恢复票务数据会让行程变 null、地图不复制。
- 本轮原话说出的人数（含中文数字，如「三个人」「我们仨」「两大一小」）必须写回 `request_context.party_size`；父方案已选车次在本轮未点名且仍适用时沿用，不能重挑。
- `mode="revise"` 只传本轮原话与 `plan_id`；`mode="confirm"` 与上一版槽位求差、按改动范围重判路由，并**先按改动失效旧确认、再应用本轮确认**。确认状态落在快照 `request_context`，不得因缺字段而静默清空。
- 仅改景点或路线时可以复用票务；出发地、目的地、日期等票务相关字段变化后不能沿用不匹配的旧票。
- 票务状态保持 `ok` / `skipped` / `unavailable` / `missed` / `timeout` / `reused` 的区别。复用票务必须标注原查询时间和余票未刷新，不能算作当前已核实。
- 车次、票价和余票以可解析的工具原始响应为依据，保留来源标识；模型总结不能直接升级为票务事实。服务失败、空结果、未调度和超时均需如实表达。
- 区分**查询事实**（解析出的行）、**适用事实**（区间/方向/日期成立的行）、**规划依据**（`plan_basis`）和**实际出行安排**（`days`）。`checks.ticket` 只看适用事实；查到车次但都不适用时为 `unverified` + `ticket_not_applicable`，不能算已核实。只有车次结果工具（`TRAIN_RESULT_TOOLS`）进车次解析，站点类查询不得产生车次诊断。
- 住宿按 `exact` / `area` / `unresolved` 三态处理。区域住宿可规划但不能宣称已确认酒店；住宿 POI 与车站不进景点候选池；候选自带的 `must_visit` 不被采纳，必去项只来自用户原话。
- 未定位项带 `kind`（`sight` / `must_visit` / `lodging` / `station`），只有 `must_visit` 让排程判 `conflict`，普通景点未定位不升级为必去冲突。
- 抵离时刻来自车次事实，`rail_window` / `day_window` 决定每天可用窗口。接驳与进站缓冲是规划假设，须随文案说明；时刻未知时窗口为 `unknown`，禁止退回默认 09:00 假装已知；窗口不足时保留为抵离日，不静默丢弃。
- 开放规则证据保留来源 URL、原文、来源标识和适用日期等字段；沿用来源白名单与冲突降级规则。未知、冲突或不适用于出行日期的证据不能变成已核实约束。
- 路线查询结果分 `ok` / `no_route` / `failed` / `not_queried`；`duration` 为 0 或缺失归 `no_route`。失败原因（`budget` / `rate_limit` / `request_failed` / `no_route`）保留到告警与技术详情，不用「未核实」一句概括。路线 `ok` 只说明「取得了有效结果」，`checks.travel_time` 用 `reference` 表示参考、只有另有完整适用性依据才升 `verified`；参考数量与已核实数量分开统计。
- 高德定位失败、开放时间未知、交通耗时未知、预算口径或人数不明确时，保留未知状态和告警；不能用模型猜测补成确定数据。
- 地图 HTML 和最终行程表使用同一份结构化 `days`。修改路线或修订复用逻辑时同时核对地图、正文、快照和方案链接，避免引用旧方案产物。旧快照（`schema_version < 4`）经 `load_snapshot_v4` 归一化；复用其行程时耗时与排程保持 `unverified`。

## 5. 成文、预算与完成状态

- 最终 Markdown 固定六个二级标题：`需求摘要`、`景点建议`、`车票建议`、`预算`、`行程表`、`注意事项`。
- 落盘前按现有流水线用结构化事实替换车票、预算、行程表，并清除事实白名单外的车次号；禁止靠放宽校验或删除告警让结果通过。
- 成文校验失败最多重试一次；再次失败或模型流等待超时后，使用已有结构化数据确定性降级。保留 `summary_gate=pass/retry_pass/degraded`。
- 外部请求、失败重试和模型调用必须受现有预算控制；失败和重试也计入工具尝试数。超时取阶段上限与剩余工作时间的较小值，不能无限等待。
- 工作预算默认 240 秒，其中最后 10 秒预留给收尾；具体以 `RunBudget` 和环境配置为准。工作截止后禁止新外部请求或模型调用，不能把预算描述为包含所有连接清理的严格墙钟承诺。
- 客户端断开或取消时及时传递 `cancel_event`、停止后续调用，并关闭模型流、子任务和本请求的 MCP 资源。首段与后续流片段都要受截止时间约束。
- `final` 表示流程结束；可靠程度由 `quality.status=complete/partial/degraded`、检查项、覆盖率和告警表达。步骤 `done`、成文闸门通过与可靠完成分别判断。
- Markdown 与修订快照成功写入后才能发送带可用产物链接的 `final`。规划记录 SQLite 写入失败按既有降级策略处理。
- token、耗时和成本保留可追溯统计；未配置有效单价时成本为未知（`cost_usd=null`），不能显示为零费用。

## 6. 接口、前端与配置

- `POST /api/plan` 是 SSE 接口。修改事件名称、字段或状态时，同步检查 `server.py`、`app.py`、`frontend/app.js`、相关测试与 README 的协议说明。
- 保持 `status`、`slots`、`clarify`、`step`、`warning`、`token`、`final`、`error` 等现有事件契约；`token` 是过程文本，最终展示与保存以 `final.content` 为准。
- 首页详细示例属于演示数据；点击「开始规划」才真实查询。示例不能包装成刚获得的实时结果。
- 展示模型 Markdown 前必须经过 DOMPurify 清理；前端脚本继续从本地 `vendor/` 加载，避免无必要的在线 CDN 或构建依赖。
- 保留 API Key、CORS、限流、并发控制与 UUID 校验等现有边界。新增接口需沿用对应约束。
- `/api/health` 仅表示进程存活；`/api/ready` 的配置检查和 MCP HTTP 探测不能证明真实模型、高德、票务工具查询成功。
- 密钥只放服务端环境变量或本地 `.env`。禁止把真实密钥写入源码、前端、文档、日志、快照或工具事件；排查时只报告是否配置，不输出值。
- `.env`、`amap-lbs-skill/config.json`、运行产物继续按 `.gitignore` 忽略。新增配置同步 `.env.example`；依赖确需变化时先取得安装确认，再更新 `requirements.txt`。
- 当前默认票务来源是提供 12306 数据的 ModelScope MCP 演示端点，不能描述为正式购票接口。
- 方案、地图、快照和规划数据库放在 `workspace/results/`；不提交真实运行数据。不擅自增加自动淘汰或批量清理用户产物的机制。

## 7. 运行与验证

使用用户已有的 Python 环境；环境缺失时先说明，不自动创建环境或安装依赖。本项目使用 Conda 环境 `travel-planner`（路径 `E:\Anaconda\envs\travel-planner`，Python 3.11.16），满足 3.11+ 要求。

启动 Web 服务时，`--reload` 依赖 uvicorn 的子进程重载机制；在受限沙箱（如本机 DSH 会话）中会因 `multiprocessing` 命名管道被拦截而持续报 `PermissionError: [WinError 5]`，导致服务无法就绪。在受限环境中改用单进程（不带 `--reload`）启动；需要热重载时，在普通终端用 `conda activate travel-planner` 后手动运行 `--reload` 命令。

```text
# Web（受限沙箱 / 不需要热重载）
conda activate travel-planner
python -m uvicorn server:app --host 127.0.0.1 --port 8000

# Web（需要热重载，在非受限终端执行）
python -m uvicorn server:app --reload --reload-exclude workspace --host 127.0.0.1 --port 8000

# CLI（会调用真实外部服务）
python app.py "这周六从新乡坐高铁去郑州一日游，预算 500，少折腾"

# 默认离线回归：与 CI 口径一致，不调用真实模型和外部工具
python -X utf8 -m pytest -q -m "not integration"
```

CI 使用 Python 3.11 / 3.12。按改动选择验证范围，避免为纯文档修改运行付费流程或无关测试。

| 改动范围 | 优先验证 |
| --- | --- |
| 槽位、路由、修订 | `evals/test_unit.py`、`evals/test_routing.py`、`evals/test_stream_plan.py`、`evals/test_revision_timeout.py` |
| 成文、事实、质量状态 | `evals/test_gate.py`、`evals/test_contracts.py`、`evals/test_quality.py` |
| 定位、取证、排程、地图一致性 | `evals/test_itinerary.py` |
| 黄金集执行与回放隔离 | `evals/test_golden_runner.py`、`evals/test_replay_isolation.py` |

- 离线测试继续使用 mock、临时结果目录、临时数据库，并关闭外部追踪。回放在成功与异常路径都要恢复临时替换的函数、目录和环境变量。
- 真实集成命令为 `python -m pytest -m integration evals/test_integration.py -q`；黄金集为 `python -m evals.run_golden`，评分为 `python -m evals.judge`。只在任务需要且已有真实调用授权时执行，先说明服务和费用用途；报告实际运行范围。
- 真跑黄金集前检查固定出行日期是否仍可查询；保留错误、超时和无终态样本，执行 `expect`，失败返回非零，不能只统计成功样本。
- 离线通过只能证明控制层和既定用例；不证明真实服务可用、模型抽取正确率、当前延迟、成本或完整线上验收。`EVALS.md` 中旧流水线成绩只作历史参考，同源模型 Judge 不视为独立评审。
- 新增回归应验证实际业务行为、失败原因或接口契约，避免只复述实现。交付时说明改动、验证结果和未验证项。

## 8. 文档维护

- 使用方式与 SSE 协议维护在 `README.md`，当前执行流程维护在 `ARCHITECTURE.md`，评测口径和真实证据维护在 `EVALS.md` / `evals/README.md`。
- 修改调度方式、工具权限、成文职责或持久化边界时同步检查 `docs/adr/`；历史决策需要标注适用范围。
- 本规范只记录长期约束；不固化某次测试数量、历史耗时、价格或临时排障结论。项目行为变化时同步更新对应条目。
