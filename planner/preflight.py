"""启动前检查必需配置。输出里不出现密钥值。"""

from __future__ import annotations

import os
import sys
from importlib.util import find_spec


def _find_spec(name: str):
    # 包一层小 helper，便于测试用 monkeypatch 替换 find_spec
    return find_spec(name)


def optional_deps() -> dict[str, str]:
    """只报告可选依赖是否可导入，不输出路径、版本或密钥。"""
    result: dict[str, str] = {}
    for name in ("deepagents", "langchain_mcp_adapters", "mcp"):
        try:
            spec = _find_spec(name)
        except (ValueError, ModuleNotFoundError):
            # find_spec 可能抛出上述异常，一律按缺失处理，不泄露异常文本
            spec = None
        result[name] = "installed" if spec is not None else "missing"
    return result


def missing_config() -> list[str]:
    missing = []
    if not (os.getenv("OPENAI_API_KEY") or "").strip():
        missing.append("OPENAI_API_KEY")
    if not ((os.getenv("AMAP_WEBSERVICE_KEY") or os.getenv("AMAP_KEY") or "").strip()):
        missing.append("AMAP_WEBSERVICE_KEY")
    return missing


def main() -> int:
    from dotenv import find_dotenv, load_dotenv

    load_dotenv(find_dotenv())
    missing = missing_config()
    if missing:
        print("缺少必需配置: " + ", ".join(missing))
        return 1
    print("必需配置已就绪")
    return 0


if __name__ == "__main__":
    sys.exit(main())
