"""
旅游规划多智能体 — 控制台入口（给命令行用的「遥控器」）

【小白怎么理解这个文件？】
    用户在黑窗口里输入一句话旅游需求。
    本文件不负责「怎么查景点/查票」，那些都在 planner_service.py。
    这里只做三件事：
        1. 读命令行参数 / 交互提问，拿到用户的一句话
        2. 把这句话交给 stream_plan()，一边跑一边把进度打印出来
        3. 如果系统说「信息不够，需要澄清」，就在终端里问用户补全，再继续跑

【和网页版的关系】
    网页版入口是 server.py + frontend/。
    两边共用同一套规划大脑：planner_service.stream_plan。
"""

# 允许使用 list[str] | None 这类现代类型写法（Python 3.10+ 风格）
from __future__ import annotations

# 解析命令行参数，例如：python app.py "去苏州玩"
import argparse
# 运行异步函数（stream_plan 是 async 的）
import asyncio
# 读环境变量（例如有没有配置 LangSmith）
import os
# 读 sys.argv（命令行参数列表）
import sys

# 真正干活的规划流水线：不断 yield 事件字典
from planner_service import stream_plan
# 旅行「槽位」数据结构：出发地/目的地/日期等；以及补默认值、查缺项
from slots import TravelSlots, apply_soft_defaults, missing_critical


def parse_query(argv: list[str] | None = None) -> str:
    """解析用户旅游需求文本。

    优先用命令行位置参数；没有参数就进入交互式 input 提问。
    例：python app.py "杭州去苏州一日游"
    """
    # 创建命令行帮助说明
    parser = argparse.ArgumentParser(description="旅游规划多智能体控制台")
    # query 是可选位置参数：写了就用，不写就是 None
    parser.add_argument("query", nargs="?", default=None, help="自然语言旅游需求")
    # 解析传入的 argv（测试时可传假参数）
    args = parser.parse_args(argv)
    # 命令行已经给了非空需求
    if args.query and args.query.strip():
        # 去掉首尾空格后返回
        return args.query.strip()
    # 没有参数：在终端提问
    try:
        # input 会阻塞等待用户输入
        query = input("请输入你的旅游需求：").strip()
    except (EOFError, KeyboardInterrupt):
        # Ctrl+C / 管道结束：友好退出
        print("\n已取消。")
        raise SystemExit(0) from None
    # 用户直接回车：视为无效
    if not query:
        raise SystemExit("未输入任何需求，已退出。")
    # 返回交互得到的需求
    return query


def _prompt_line(label: str, default: str | None = None) -> str | None:
    """在终端问一行，允许用默认值。

    用户直接回车 → 返回 default；输入了内容 → 返回输入。
    """
    # 有默认值时在提示里显示出来，方便用户直接回车
    hint = f"（默认 {default}）" if default not in (None, "") else ""
    try:
        # 读一行用户输入并去掉空白
        raw = input(f"{label}{hint}：").strip()
    except (EOFError, KeyboardInterrupt):
        # 用户中断输入
        print("\n已取消。")
        raise SystemExit(0) from None
    # 用户输入了非空内容
    if raw:
        return raw
    # 空输入则沿用默认（可能仍是 None）
    return default


def collect_slots_interactively(event: dict) -> TravelSlots:
    """根据后端发来的 clarify 事件，在终端里补全/确认槽位。

    clarify 事件里通常带有：
        - slots：当前已猜到的字段
        - missing：还缺哪些关键字段（出发地/目的地/日期）
        - defaults_applied：哪些软字段用了默认值（天数/预算等）
    """
    # 以事件里已有槽位为底稿（可能不全）
    base = dict(event.get("slots") or {})
    # 关键缺失字段名集合，方便后面校验
    missing = set(event.get("missing") or [])
    # 哪些字段是系统预填的默认值
    defaults_applied = set(event.get("defaults_applied") or [])
    # 打印说明，让用户知道为什么要再填一遍
    print("\n—— 需要确认的旅行参数 ——")
    if missing:
        print("关键缺失：", "、".join(missing))
    if defaults_applied:
        print("已预填默认（可改）：", "、".join(defaults_applied))

    # 字段英文名 → 中文提问文案
    labels = {
        "origin": "出发地",
        "destination": "目的地",
        "date": "出行日期 YYYY-MM-DD",
        "days": "游玩天数",
        "budget": "预算",
        "preferences": "偏好",
        "pace": "出行节奏",
    }
    # 按固定顺序逐个询问，避免漏字段
    for key in ("origin", "destination", "date", "days", "budget", "preferences", "pace"):
        # 当前已有值（可能来自抽取或默认）
        current = base.get(key)
        # 转成字符串给提示用；None 就显示为空默认
        default_str = "" if current is None else str(current)
        # 关键字段不允许空：用 while 反复问到合法为止
        while True:
            value = _prompt_line(labels[key], default_str or None)
            # 关键缺失且用户仍给空 → 继续问
            if key in missing and not value:
                print("该项为关键信息，不能为空。")
                continue
            # 天数必须是正整数
            if key == "days" and value is not None:
                try:
                    base[key] = int(value)
                except ValueError:
                    print("天数请输入正整数。")
                    continue
            else:
                # 其它字段直接存字符串或 None
                base[key] = value
            # 本字段合法，跳出 while 问下一个
            break

    # 用 Pydantic 校验整体结构（日期格式等）
    slots = TravelSlots.model_validate(base)
    # 若校验后关键字段仍缺：递归再问一轮（极少见）
    if missing_critical(slots):
        return collect_slots_interactively(
            {
                "slots": apply_soft_defaults(slots)[0].model_dump(),
                "missing": missing_critical(slots),
                "defaults_applied": [],
            }
        )
    # 软字段补默认后返回最终确认槽位
    filled, _ = apply_soft_defaults(slots)
    return filled


def collect_pending_interactively(pending: list[dict] | None, base: dict) -> dict:
    """终端里处理 clarify 事件带下来的「待确认项」。

    网页端弹窗问的就是这三类：住宿只给到区域、几人均摊、预算口径。
    这里同样问一遍，并把答案变成请求字段随下一轮传回（`mode="confirm"`），
    否则命令行每轮都会重新澄清同一件事。
    """
    fields: dict[str, object] = {}
    if not pending:
        return fields
    print("\n—— 需要你确认的项 ——")
    for item in pending:
        field = str(item.get("field") or "")
        message = str(item.get("message") or "")
        if field == "lodging":
            print(f"住宿：{message}")
            choice = _prompt_line("怎么处理住宿？1=填具体酒店，2=按该区域参考规划", "2")
            if str(choice).strip() == "1":
                hotel = _prompt_line("具体酒店名称", base.get("lodging") or None)
                if hotel:
                    # 住宿文本本身也要跟着改，否则确认只改了口径、酒店名还是原来那个区域。
                    fields["lodging"] = str(hotel)
                    fields["lodging_scope"] = "exact"
            else:
                fields["lodging_scope"] = "area"
        elif field == "party_size":
            print(f"人数：{message}")
            while True:
                raw = _prompt_line("一共几个人", None)
                try:
                    count = int(str(raw).strip())
                except (TypeError, ValueError):
                    print("人数请输入正整数。")
                    continue
                if count <= 0:
                    print("人数请输入正整数。")
                    continue
                fields["party_size"] = count
                break
        elif field == "budget_scope":
            print(f"预算口径：{message}")
            choice = _prompt_line("1=总额，2=人均", "1")
            fields["budget_scope"] = "per_person" if str(choice).strip() == "2" else "total"
    return fields


async def run_once(
    query: str,
    slots: TravelSlots | None = None,
    parent_plan_id: str | None = None,
    *,
    mode: str = "plan",
    lodging_scope: str | None = None,
    party_size: int | None = None,
    budget_scope: str | None = None,
) -> dict | None:
    """跑一轮规划流水线，并把各类事件打印到控制台。

    返回值：
        - 若中途收到 clarify：返回该事件 dict，让 main 去交互补槽
        - 若正常跑完或出错：返回 None
    """
    # 最终方案正文（收到 final 事件时赋值）
    final_event: dict | None = None
    final_answer = None
    clarify_event: dict | None = None
    async for event in stream_plan(
        query,
        slots=slots,
        parent_plan_id=parent_plan_id,
        mode=mode,
        lodging_scope=lodging_scope,
        party_size=party_size,
        budget_scope=budget_scope,
    ):
        # 事件类型：status / step / clarify / token / final ...
        etype = event.get("type")
        if etype == "status":
            # 人类可读的阶段说明
            print(f"[状态]:{event.get('message')}")
        elif etype == "step":
            extra = ""
            if event.get("reason"):
                extra += f" reason={event.get('reason')}"
            if event.get("elapsed_ms") is not None:
                extra += f" {event.get('elapsed_ms')}ms"
            print(f"[步骤]:{event.get('id')} -> {event.get('status')}{extra}")
        elif etype == "slots":
            # 系统抽取出的槽位快照
            print(f"[槽位]:{event.get('slots')}")
        elif etype == "clarify":
            # 需要用户补信息：先记下，等本轮生成器结束后处理
            clarify_event = event
            print(f"[澄清]:{event.get('message')}")
        elif etype == "subagent":
            # 主智能体决定调用哪个专家子智能体
            print(f"[模型决定调用子智能体],智能体:{event.get('name')}")
        elif etype == "tool":
            # 调用了普通工具（非 task 委派）
            print(f"[模型决定调用工具],工具:{event.get('name')},传入参数:{event.get('args')}")
        elif etype == "model":
            # 模型中间说话；太长就截断，避免刷屏
            content = str(event.get("content") or "")
            if len(content) > 200:
                content = content[:200] + "..."
            print(f"[模型]:{content}")
        elif etype == "tool_result":
            print(f"[执行工具返回结果]:{event.get('content')}")
        elif etype == "warning":
            print(f"[警告]:{event.get('message')}")
        elif etype == "token":
            # 汇总阶段的逐字流式输出：不换行，边到边打
            print(event.get("content") or "", end="", flush=True)
        elif etype == "final":
            # 全部完成：换行后打印落盘/地图/追踪链接
            print()
            final_event = event
            final_answer = event.get("content")
            saved_url = event.get("saved_url")
            map_url = event.get("map_url")
            trace_url = event.get("trace_url")
            timings = event.get("timings")
            if saved_url:
                print(f"[落盘]:{saved_url}")
            if map_url:
                print(f"[地图]:{map_url}")
            if timings:
                print(f"[耗时]:{timings}")
            quality = event.get("quality") or {}
            if quality:
                print(f"[质量]:{quality.get('status')}")
                for issue in quality.get("issues") or []:
                    print(f"[质量问题]:{issue.get('message')}")
            if trace_url:
                print(f"[LangSmith]:{trace_url}")
            elif os.getenv("LANGSMITH_API_KEY"):
                # 有 Key 但没拼出 URL 时，给官网入口兜底提示
                print("[LangSmith]:请到 https://smith.langchain.com 项目 travel-planner 查看本次追踪")
        elif etype == "error":
            # 出错：打印后结束本轮
            print(f"[错误]:{event.get('message')}")
            return None

    # 若本轮是「请澄清」而不是「已完成」
    if clarify_event is not None:
        return clarify_event

    # 正常结束：打印最终方案
    print("\n========== 最终结果 ==========\n")
    print(final_answer or "本次没有生成最终结果")
    return final_event


async def _confirm_clarify(
    pending: dict,
    query: str,
    parent_plan_id: str | None,
) -> dict | None:
    """终端里补完一轮 clarify 后，以 `mode="confirm"` 回传。

    `query` 与 `parent_plan_id` 保留本轮上下文：首次规划澄清 → 原需求 + 无父方案；
    修订触发澄清 → 原修改意见 + 父方案 ID。这样后端才能把这份槽位与父方案求差，
    保留修改意图，也不会让旧槽位覆盖新意图。
    """
    confirmed = collect_slots_interactively(pending)
    # 澄清事件里的「待确认项」在命令行同样要问：否则每轮都重新澄清同一件事。
    extra = collect_pending_interactively(pending.get("pending"), confirmed.model_dump())
    # 住宿文本是槽位，口径/人数是请求字段：前者并进 slots，后者留在 extra 里透传。
    hotel = extra.pop("lodging", None)
    if hotel:
        confirmed = TravelSlots.model_validate({**confirmed.model_dump(), "lodging": hotel})
    print("\n========== 确认后继续 ==========\n")
    return await run_once(
        query,
        slots=confirmed,
        parent_plan_id=parent_plan_id,
        mode="confirm",
        **extra,
    )


async def main(argv: list[str] | None = None) -> None:
    """入口：先规划；若要澄清则终端补槽再跑；完成后可循环输入修订意见。

    澄清可能在首次规划或任意一轮修订中出现，且确认后仍可能再次返回 clarify，
    所以用两个 `while` 循环一直处理到拿到 final 或用户结束为止，而不是只补一次。
    """
    query = parse_query(argv if argv is not None else sys.argv[1:])
    print("\n========== 开始规划 ==========\n")
    parent_plan_id: str | None = None
    pending = await run_once(query, slots=None)
    # 首次规划的澄清：补槽后以 confirm 续跑（无父方案）。
    while pending is not None and pending.get("type") == "clarify":
        pending = await _confirm_clarify(pending, query, parent_plan_id)
    while pending is not None and pending.get("type") == "final" and pending.get("plan_id"):
        plan_id = str(pending.get("plan_id"))
        try:
            revision = input("\n输入修改意见（直接回车结束）：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已结束。")
            return
        if not revision:
            return
        print("\n========== 修订方案 ==========\n")
        pending = await run_once(revision, parent_plan_id=plan_id, mode="revise")
        # 修订也可能触发澄清：补槽后以 confirm + 原修改意见 + 父方案 ID 续跑。
        while pending is not None and pending.get("type") == "clarify":
            pending = await _confirm_clarify(pending, revision, plan_id)


# 只有「直接 python app.py」时才启动；被 import 时不自动跑
if __name__ == "__main__":
    try:
        # 把异步 main 丢进事件循环
        asyncio.run(main())
    except KeyboardInterrupt:
        # Ctrl+C 优雅退出
        print("\n已取消。")
        raise SystemExit(0) from None
