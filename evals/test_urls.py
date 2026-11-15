"""外部 URL 校验的回归：危险 scheme、凭据、本机地址、畸形输入。"""

from __future__ import annotations

import pytest

from planner.urls import MAX_URL_LENGTH, safe_evidence_url, safe_http_url

HOSTS = frozenset({"www.dpm.org.cn", "museum.example"})


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://www.dpm.org.cn/visit", "https://www.dpm.org.cn/visit"),
        ("http://museum.example/hours", "http://museum.example/hours"),
        ("https://www.dpm.org.cn", "https://www.dpm.org.cn/"),
        ("HTTPS://WWW.DPM.ORG.CN/a", "https://www.dpm.org.cn/a"),
        ("https://www.dpm.org.cn./a", "https://www.dpm.org.cn/a"),
        ("https://www.dpm.org.cn/a?x=1#y", "https://www.dpm.org.cn/a?x=1#y"),
        ("https://www.dpm.org.cn:443/a", "https://www.dpm.org.cn/a"),
    ],
)
def test_safe_http_url_keeps_regular_urls(raw, expected):
    assert safe_http_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "file:///c:/windows/win.ini",
        "ftp://www.dpm.org.cn/a",
        "//evil.example/a",
        "",
        "   ",
        None,
        123,
        "https://user:pass@www.dpm.org.cn/a",
        "https://user@www.dpm.org.cn/a",
        "https://www.dpm.org.cn:8443/a",
        "http://www.dpm.org.cn:443/a",
        "http://127.0.0.1/a",
        "http://localhost/a",
        "http://[::1]/a",
        "http://10.0.0.1/a",
        "http://192.168.1.1/a",
        "http://169.254.1.1/a",
        "http://[::ffff:127.0.0.1]/a",
        "https://[not-ipv6/a",
        "https://www.dpm.org.cn/a\nSet-Cookie: x=1",
        "https://www.dpm.org.cn/a\r\n",
        "https://" + "a" * MAX_URL_LENGTH + ".com/",
    ],
)
def test_safe_http_url_rejects_unsafe(raw):
    assert safe_http_url(raw) is None


def test_safe_evidence_url_requires_whitelist_membership():
    assert safe_evidence_url("https://www.dpm.org.cn/visit", HOSTS) == "https://www.dpm.org.cn/visit"
    # 合法 http(s) 但不在白名单：放行会破坏"来源可信"的前提
    assert safe_evidence_url("https://evil.example/a", HOSTS) is None


def test_safe_evidence_url_rejects_when_whitelist_empty():
    """白名单必填，空集合等于"没有可信来源"，绝不默认放行。"""
    assert safe_evidence_url("https://www.dpm.org.cn/visit", frozenset()) is None
    assert safe_evidence_url("https://www.dpm.org.cn/visit", []) is None
    assert safe_evidence_url("https://www.dpm.org.cn/visit", None) is None


def test_safe_evidence_url_case_insensitive_host():
    assert safe_evidence_url("https://WWW.DPM.ORG.CN/x", {"WWW.DPM.ORG.CN"}) == "https://www.dpm.org.cn/x"


def test_parse_evidence_drops_unsafe_source_url():
    """来源链接不合格时不留可点击地址，整条事实降级为 pending。"""
    from planner.travel_search import parse_evidence

    facts = parse_evidence(
        "甲博物馆每周一闭馆。",
        url="javascript:alert(1)",
        poi_name="甲博物馆",
        poi_id="jia",
        allowed_hosts=frozenset({"museum.example"}),
    )
    assert facts, "事实本身仍应被解析出来"
    assert facts[0]["source_url"] == ""
    assert facts[0]["status"] == "pending"
    assert facts[0]["source_url_raw"] == "javascript:alert(1)"


def test_parse_evidence_keeps_whitelisted_source_url():
    from planner.travel_search import parse_evidence

    facts = parse_evidence(
        "甲博物馆每周一闭馆。",
        url="https://museum.example/hours",
        poi_name="甲博物馆",
        poi_id="jia",
        allowed_hosts=frozenset({"museum.example"}),
    )
    assert facts[0]["status"] == "hard"
    assert facts[0]["source_url"] == "https://museum.example/hours"
    assert "source_url_raw" not in facts[0]


def test_map_context_drops_foreign_evidence_links():
    """进成文提示词的证据链接同样只保留白名单内地址。"""
    from planner.map_fallback import _context

    plan = {"days": [], "schedule_status": "candidate"}
    context = _context(
        plan,
        [],
        "ok",
        None,
        facts=[
            {"source_url": "https://www.dpm.org.cn/visit"},
            {"source_url": "https://evil.example/a"},
            {"source_url": "javascript:alert(1)"},
        ],
    )
    assert "https://www.dpm.org.cn/visit" in context
    assert "evil.example" not in context
    assert "javascript:" not in context
