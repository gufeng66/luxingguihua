"""确认每次票务连接都是新的。不进 CI。

用法：python -m evals.cache_probe
"""

from __future__ import annotations

import asyncio
import json

from planner.ticket_cache import close_ticket_agent, get_ticket_agent


async def main() -> int:
    first = await get_ticket_agent()
    second = await get_ticket_agent()
    print("first", json.dumps({"agent": first is not None, **((first or {}).get("diag") or {})}, ensure_ascii=False))
    print("second", json.dumps({"agent": second is not None, **((second or {}).get("diag") or {})}, ensure_ascii=False))
    await close_ticket_agent(first)
    await close_ticket_agent(second)
    if first is None or second is None:
        return 2
    return 0 if first is not second else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
