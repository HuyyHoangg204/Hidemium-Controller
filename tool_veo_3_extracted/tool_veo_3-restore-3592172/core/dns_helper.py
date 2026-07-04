"""
DNS-over-HTTPS helper để bypass system DNS.

Khi system DNS không resolve được labs.google (do mạng chập chờn, ISP chặn,
v.v.), module này sẽ resolve qua DoH (8.8.8.8, 1.1.1.1) và cache kết quả.
IP cache được dùng làm argument `resolve` cho curl_cffi để bỏ qua system DNS.
"""

import json
import socket
import concurrent.futures
from typing import Optional

# Cache IP đã resolve
_RESOLVED_IPS: dict = {}

# DoH endpoints (thử theo thứ tự)
_DOH_SERVERS = [
    "https://8.8.8.8/resolve",
    "https://1.1.1.1/dns-query",
    "https://dns.google/resolve",
]


def resolve_via_doh(hostname: str, timeout: int = 5, proxy: str = None) -> Optional[str]:
    """Resolve hostname qua DNS-over-HTTPS, trả về IPv4 string hoặc None.

    DoH request đi qua HTTPS port 443 nên không bị block bởi DNS filter.
    Không phụ thuộc vào system DNS của máy.
    Nếu proxy được truyền vào, DoH request cũng đi qua proxy.
    """
    import urllib.request
    import urllib.parse

    for doh_url in _DOH_SERVERS:
        try:
            params = urllib.parse.urlencode({"name": hostname, "type": "A"})
            req = urllib.request.Request(
                f"{doh_url}?{params}",
                headers={"Accept": "application/dns-json"},
            )
            if proxy:
                proxy_handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
                opener = urllib.request.build_opener(proxy_handler)
                with opener.open(req, timeout=timeout) as resp:  # opener.open() not .urlopen()
                    data = json.loads(resp.read().decode())
            else:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode())
            answers = data.get("Answer", []) or data.get("answer", [])
            for ans in answers:
                if ans.get("type") == 1:  # A record
                    ip = str(ans.get("data", "")).strip()
                    if ip:
                        print(f"[DNS] {hostname} -> {ip} (via DoH {doh_url})")
                        return ip
        except Exception as e:
            print(f"[DNS] DoH {doh_url} failed for {hostname}: {e}")
            continue
    return None


def get_ip(hostname: str, timeout: int = 5, proxy: str = None) -> Optional[str]:
    """Lấy IP của hostname: cache -> DoH -> system DNS (fallback).

    Kết quả được cache trong session để tránh resolve nhiều lần.
    Cache key tách biệt theo proxy để tránh dùng IP cache sai.
    """
    # Cache key theo hostname + proxy
    cache_key = f"{hostname}@{proxy or ''}"
    if cache_key in _RESOLVED_IPS:
        return _RESOLVED_IPS[cache_key]

    # Ưu tiên DoH (không phụ thuộc system DNS, hỗ trợ proxy)
    ip = resolve_via_doh(hostname, timeout=timeout, proxy=proxy)
    if ip:
        _RESOLVED_IPS[cache_key] = ip
        return ip

    # Fallback: system DNS với thread timeout (tránh block vô hạn trên Windows)
    def _system_dns():
        return socket.getaddrinfo(hostname, 443, socket.AF_INET)[0][4][0]

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            ip = ex.submit(_system_dns).result(timeout=timeout)
        if ip:
            _RESOLVED_IPS[cache_key] = ip
            return ip
    except Exception:
        pass

    return None


def get_curl_resolve(hostname: str, port: int = 443, timeout: int = 5, proxy: str = None) -> Optional[list]:
    """Trả về list `resolve` cho curl_cffi để bypass system DNS.

    Khi có proxy: proxy tự xử lý DNS → skip DoH /resolve, trả None.
    Khi không có proxy: resolve qua DoH để bypass system DNS bị block.
    Trả về None nếu không resolve được (caller tự xử lý).
    """
    # Khi có proxy: proxy tự resolve DNS, KHÔNG cần DoH → skip hoàn toàn
    if proxy:
        return None

    ip = get_ip(hostname, timeout=timeout, proxy=None)
    if ip:
        return [f"{hostname}:{port}:{ip}"]
    return None


def invalidate_cache(hostname: str = None):
    """Xoá cache IP (gọi khi cần re-resolve)."""
    if hostname:
        _RESOLVED_IPS.pop(hostname, None)
    else:
        _RESOLVED_IPS.clear()
