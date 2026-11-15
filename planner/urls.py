"""外部 URL 的统一校验。

【小白怎么理解？】
    项目里会把「搜索到的网页链接」带到前端给人点、也会拼进给模型的提示词。
    如果这条链接是 `javascript:`、`data:`、带 `user:pass@` 的地址、指向本机
    或内网的地址，点一下就可能变成在用户浏览器里执行脚本或探测内网。
    所以所有要外显或外传的 URL 都必须先过这里。

两个函数职责分开，避免"忘了传白名单 = 不检查"这种默认放行：

- `safe_http_url(raw)`：只保证"这是一个规规矩矩的公网 http(s) 地址"。
- `safe_evidence_url(raw, allowed_hosts)`：在上一层的检查之外，**强制**要求
  主机名在来源白名单里。allowed_hosts 是必填参数，不传就是调用方的 bug。

这里不联网，纯粹是字符串与主机名判定，方便单测。
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

# 网址长度上限。真实网页链接极少超过这个数，超长的多半是拼接注入。
MAX_URL_LENGTH = 2048

# 允许的 scheme。data:/javascript:/file:/ftp: 等一律拒绝。
ALLOWED_SCHEMES = frozenset({"http", "https"})

# 各 scheme 的默认端口。写了非默认端口的一律拒绝：本项目不存在需要访问
# 高位端口的证据来源，放行只会扩大攻击面。
DEFAULT_PORTS = {"http": 80, "https": 443}

# 控制字符与空白。URL 里出现这些通常意味着换行注入或日志污染。
_CONTROL = re.compile(r"[\x00-\x20\x7f]")
# 换行与制表符单独再判一次：前后空白允许，中间/尾部的换行不允许，
# 否则 `https://a/b\r\nSet-Cookie: x=1` 会被 strip 成"看起来正常"的地址。
_NEWLINE = re.compile(r"[\r\n\t]")

# 127.0.0.1 / ::1 / localhost 这类本机地址
_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})


def _normalize_host(host: str) -> str:
    """主机名统一小写并去掉尾部点号（`Example.com.` 与 `example.com` 等价）。"""
    return host.strip().lower().rstrip(".")


def _private_host(host: str) -> bool:
    """判断是否本机或内网地址。"""
    if not host:
        return True
    if host in _LOOPBACK_NAMES or host.endswith(".localhost"):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        # 不是 IP 字面量，按普通域名放行（域名是否真实存在不在这里判断）
        return False
    return addr.is_loopback or addr.is_private or addr.is_link_local or addr.is_reserved or addr.is_unspecified


def safe_http_url(raw: object) -> str | None:
    """返回规范化后的 http(s) URL；不安全或不可解析时返回 None。

    检查项：长度、控制字符、scheme、主机名为空、userinfo（user:pass@）、
    非默认端口、本机/内网地址、非法 IPv6 或被 urlsplit 拒绝的畸形输入。
    """
    if not isinstance(raw, str):
        return None
    # 换行/制表符在 strip 之前查：`https://a/b\r\nSet-Cookie: x=1` 去掉尾部
    # 空白后会变成"看起来正常"的地址，先拒绝才安全。
    if _NEWLINE.search(raw):
        return None
    text = raw.strip()
    if not text or len(text) > MAX_URL_LENGTH:
        return None
    if _CONTROL.search(text):
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        # urlsplit 遇到畸形 IPv6 括号、非法端口会抛 ValueError
        return None
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        return None
    if parts.username or parts.password:
        # http://user:pass@example.com 一律拒绝：证据链接不需要凭据
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if port is not None and port != DEFAULT_PORTS[scheme]:
        return None
    host = _normalize_host(parts.hostname or "")
    if not host or _private_host(host):
        return None
    # 端口与主机名都规范化后重新拼回，保证下游拿到的是同一个形态。
    # 显式写了默认端口（https://a:443/、http://a:80/）时省掉，避免同一地址
    # 出现两种字符串形态，导致去重和比对失效。
    netloc = host if port is None or port == DEFAULT_PORTS[scheme] else f"{host}:{port}"
    path = parts.path or "/"
    rebuilt = f"{scheme}://{netloc}{path}"
    if parts.query:
        rebuilt += f"?{parts.query}"
    if parts.fragment:
        rebuilt += f"#{parts.fragment}"
    return rebuilt


def normalize_hosts(hosts) -> frozenset[str]:
    """把白名单集合规范化，便于与 `safe_http_url` 的主机名比较。"""
    return frozenset(_normalize_host(str(host)) for host in (hosts or ()) if str(host).strip())


def safe_evidence_url(raw: object, allowed_hosts) -> str | None:
    """证据链接专用：必须是公网 http(s)，且主机名在白名单内。

    `allowed_hosts` 必填。传空集合等于"没有任何来源可信"，返回 None —— 这是
    刻意的：宁可少显示一条链接，也不要默认放行任意外部地址。
    """
    hosts = normalize_hosts(allowed_hosts)
    if not hosts:
        return None
    url = safe_http_url(raw)
    if url is None:
        return None
    host = _normalize_host(urlsplit(url).hostname or "")
    if host not in hosts:
        return None
    return url
