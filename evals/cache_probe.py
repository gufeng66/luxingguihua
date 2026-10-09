"""同一进程里测车票 MCP 冷热握手。不进 CI。

用法：python -m evals.cache_probe
第一次必须拿到 agent 且 cache_hit 为 false，第二次才算热缓存。
冷握手失败会进 30 秒冷却，第二次返回 None，不是命中。
"""

from __future__ import annotations

import asyncio
import json

from planner.ticket_cache import get_ticket_agent, last_lookup


async def main() -> int:
    first = await get_ticket_agent()
    cold = last_lookup()
    print("cold", json.dumps({"agent": first is not None, **cold}, ensure_ascii=False))
    if first is None or cold.get("cache_hit"):
        print("hot skipped")
        return 2
    second = await get_ticket_agent()
    hot = last_lookup()
    print("hot", json.dumps({"agent": second is not None, **hot}, ensure_ascii=False))
    return 0 if second is not None and hot.get("cache_hit") else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
