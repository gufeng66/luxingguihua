"""可选依赖诊断与 create_deep_agent 历史符号的离线单测（不联网、不 import deepagents）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import planner.preflight as pf  # noqa: E402
import planner_service as ps  # noqa: E402

_DEPS = ("deepagents", "langchain_mcp_adapters", "mcp")


def test_optional_deps_all_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """find_spec 均返回非 None 时，三项都为 installed。"""
    monkeypatch.setattr(pf, "_find_spec", lambda name: object())
    assert pf.optional_deps() == {
        "deepagents": "installed",
        "langchain_mcp_adapters": "installed",
        "mcp": "installed",
    }


@pytest.mark.parametrize("missing_name", _DEPS)
def test_optional_deps_each_missing(monkeypatch: pytest.MonkeyPatch, missing_name: str) -> None:
    """逐个缺失时只有对应项为 missing，其余仍为 installed。"""
    def fake_find_spec(name: str):
        return None if name == missing_name else object()

    monkeypatch.setattr(pf, "_find_spec", fake_find_spec)
    result = pf.optional_deps()
    assert result[missing_name] == "missing"
    for name in _DEPS:
        if name != missing_name:
            assert result[name] == "installed"


@pytest.mark.parametrize("exc", [ValueError("bad spec"), ModuleNotFoundError("no module")])
def test_optional_deps_find_spec_raises(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    """find_spec 抛 ValueError/ModuleNotFoundError 也按 missing 处理，且不泄露异常文本。"""
    def fake_find_spec(name: str):
        raise exc

    monkeypatch.setattr(pf, "_find_spec", fake_find_spec)
    result = pf.optional_deps()
    assert result == {
        "deepagents": "missing",
        "langchain_mcp_adapters": "missing",
        "mcp": "missing",
    }
    assert "bad spec" not in repr(result)
    assert "no module" not in repr(result)


def test_optional_deps_emits_no_paths_versions_or_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """即使 spec 带路径/版本/密钥类信息，输出也只含包名与 installed/missing 状态。"""
    class FakeSpec:
        origin = "C:/secrets/site-packages/deepagents/__init__.py"
        version = "9.8.7"
        secret = "sk-supersecrettoken"

    monkeypatch.setattr(pf, "_find_spec", lambda name: FakeSpec())
    result = pf.optional_deps()

    assert set(result) == set(_DEPS)
    assert all(v in {"installed", "missing"} for v in result.values())
    text = repr(result)
    for forbidden in (
        "C:/",
        "site-packages",
        "__init__.py",
        "9.8.7",
        "sk-",
        "supersecret",
        "Traceback",
        "OPENAI_API_KEY",
    ):
        assert forbidden not in text


@pytest.mark.asyncio
async def test_ready_exposes_optional_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    """/api/ready 直接调用：只读报告可选依赖状态，不联网、不泄露路径/版本/密钥。"""
    import server

    # 受控假探测：断言永不触发真实 MCP/网络访问
    def fake_probe(_url: str) -> bool:
        return False

    monkeypatch.setattr(server, "_probe_mcp", fake_probe)
    body = await server.ready()

    # 1) 返回含 optional_deps 键且是 dict
    assert "optional_deps" in body
    deps = body["optional_deps"]
    assert isinstance(deps, dict)
    # 2) 三个键齐全，值只能是 installed / missing（不硬编码具体环境）
    assert set(deps) == set(_DEPS)
    assert all(v in {"installed", "missing"} for v in deps.values())
    # 3) 与直接调用结果一致
    assert deps == pf.optional_deps()
    # 4) 隐私约束：输出里没有文件路径、版本号、密钥字样
    text = repr(deps)
    for forbidden in (
        "C:/",
        "C:\\",
        "site-packages",
        "__init__.py",
        "9.8.7",
        "sk-",
        "supersecret",
        "OPENAI_API_KEY",
        "AMAP_WEBSERVICE_KEY",
        "Traceback",
    ):
        assert forbidden not in text
    # 5) 不触真实网络：_probe_mcp 已被受控假函数替换
    assert server._probe_mcp is fake_probe


def test_create_deep_agent_missing_raises_runtimeerror(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺 deepagents 状态下调用历史符号，抛 RuntimeError 且消息完全匹配契约。"""
    monkeypatch.setattr(ps, "_real_create_deep_agent", None)
    assert "create_deep_agent" in ps.__all__
    with pytest.raises(RuntimeError) as err:
        ps.create_deep_agent(tools=[], system_prompt="x")
    assert str(err.value) == "deepagents 未安装，当前主路径不使用 create_deep_agent"


def test_create_deep_agent_delegates_when_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """已安装状态下包装函数按原参数委托给真正的 create_deep_agent。"""
    seen: dict[str, object] = {}

    def fake_create(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return "agent-instance"

    monkeypatch.setattr(ps, "_real_create_deep_agent", fake_create)
    assert ps.create_deep_agent("a", tools=["t"]) == "agent-instance"
    assert seen["args"] == ("a",)
    assert seen["kwargs"] == {"tools": ["t"]}
