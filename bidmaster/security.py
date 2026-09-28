"""URL 安全校验：防 SSRF（MCP file_url / A2A file.uri 的外部拉取入口）。

规则：
  - 仅允许 http/https；
  - 拒绝主机名解析到私网/环回/链路本地地址的目标（含 IP 直连写法）；
  - 拒绝云元数据地址（169.254.169.254 等）。
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class URLNotAllowed(ValueError):
    pass


def _strict_dns() -> bool:
    from bidmaster.config import get_settings
    return get_settings().url_strict_dns


def _is_private_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_multicast)


def validate_public_url(url: str, *, resolve_dns: bool | None = None) -> str:
    """校验外部拉取 URL；不合法抛 URLNotAllowed。返回净化后的 URL。"""
    u = urlparse(url or "")
    if u.scheme not in ("http", "https"):
        raise URLNotAllowed(f"仅允许 http/https 协议: {u.scheme!r}")
    host = u.hostname or ""
    if not host:
        raise URLNotAllowed("URL 缺少主机名")
    # IP 直连写法直接判；域名再走 DNS 解析防"域名解析到内网"绕过
    try:
        ipaddress.ip_address(host)
        literal = True
    except ValueError:
        literal = False
    if literal:
        if _is_private_ip(host):
            raise URLNotAllowed(f"禁止访问内网/保留地址: {host}")
    elif resolve_dns if resolve_dns is not None else _strict_dns():
        try:
            infos = socket.getaddrinfo(host, u.port or (443 if u.scheme == "https" else 80),
                                       proto=socket.IPPROTO_TCP)
        except (socket.gaierror, OSError) as e:
            raise URLNotAllowed(f"主机名解析失败: {host} ({e})")
        for info in infos:
            ip = info[4][0]
            if _is_private_ip(ip):
                raise URLNotAllowed(f"主机 {host} 解析到内网/保留地址 {ip}，已拦截")
    # 常见云元数据端点
    if host in ("169.254.169.254", "metadata.google.internal"):
        raise URLNotAllowed("禁止访问云元数据地址")
    return url
