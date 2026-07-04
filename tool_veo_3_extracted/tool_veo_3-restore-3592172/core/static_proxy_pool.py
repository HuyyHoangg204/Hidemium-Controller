"""
Static Proxy Pool.

Đọc proxy từ file proxy.txt ở root project. Mỗi dòng một proxy.
Hỗ trợ format host:port:user:pass và HTTP|host|port|user|pass.
"""

import os
import random
import threading
import logging
import time

logger = logging.getLogger(__name__)

_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_POOL_FILE = os.path.join(_ROOT_DIR, "proxy.txt")
_LEGACY_POOL_FILE = os.path.join(_ROOT_DIR, "proxy_pool.txt")

_lock = threading.Lock()
_cached_proxies: list = []
_last_load_ts: float = 0
_CACHE_TTL = 60  # Reload file mỗi 60s

# ── Upload retry: shared dead set có TTL ──
# Khi upload fail vì proxy → mark proxy là dead trong UPLOAD_DEAD_TTL giây.
# Mọi task tránh proxy này trong khoảng đó. Hết TTL → cho cơ hội thử lại.
UPLOAD_DEAD_TTL = 60          # 1 phút — chỉnh constant này nếu cần
UPLOAD_MAX_PICK_TRIES = 200   # safety: số lần loop pick fresh proxy trong 1 vòng

_dead_lock = threading.Lock()
_dead_proxies: dict = {}      # {proxy_url: expire_ts}


def _parse_line(line: str) -> "str | None":
    """Parse 1 dòng proxy → http://user:pass@host:port. Trả None nếu invalid.

    Hỗ trợ 2 format:
      Format 1 (pipe):  HTTP|host|port|user|pass
      Format 2 (colon): host:port:user:pass
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    # ── Format 1: pipe-delimited  (HTTP|host|port|user|pass) ──
    if "|" in line:
        parts = line.split("|")
        if len(parts) < 5:
            return None
        _, host, port, user, passwd = parts[0], parts[1], parts[2], parts[3], parts[4]
        return f"http://{user}:{passwd}@{host}:{port}"

    # ── Format 2: colon-delimited (host:port:user:pass) ──
    parts = line.split(":")
    if len(parts) == 4:
        host, port, user, passwd = parts
        return f"http://{user}:{passwd}@{host}:{port}"

    return None


def _load_pool():
    """Load proxy.txt, fallback proxy_pool.txt nếu file mới chưa có."""
    global _cached_proxies, _last_load_ts
    now = time.time()
    with _lock:
        if _cached_proxies and now - _last_load_ts < _CACHE_TTL:
            return
        pool_file = _POOL_FILE if os.path.exists(_POOL_FILE) else _LEGACY_POOL_FILE
        proxies = []
        try:
            if os.path.exists(pool_file):
                with open(pool_file, "r", encoding="utf-8") as handle:
                    for line in handle:
                        parsed = _parse_line(line)
                        if parsed:
                            proxies.append(parsed)
            else:
                logger.warning("Static proxy file not found: %s", pool_file)
        except Exception as exc:
            logger.warning("Failed to load proxy pool %s: %s", pool_file, exc)
        _cached_proxies = proxies
        _last_load_ts = now
        logger.info("Loaded %d static proxies from %s", len(_cached_proxies), pool_file)


def get_random_proxy() -> "str | None":
    _load_pool()
    with _lock:
        if not _cached_proxies:
            return None
        return random.choice(_cached_proxies)


def get_all_proxies() -> list[str]:
    """Trả về danh sách proxy theo đúng thứ tự dòng trong proxy.txt."""
    _load_pool()
    with _lock:
        return list(_cached_proxies)


def pool_size() -> int:
    _load_pool()
    with _lock:
        return len(_cached_proxies)


# ── Upload-retry helpers ─────────────────────────────────────────────────────
def _cleanup_expired_dead():
    """Xóa entry hết hạn khỏi _dead_proxies. CALLER PHẢI HOLD _dead_lock."""
    now = time.time()
    expired = [p for p, exp in _dead_proxies.items() if exp <= now]
    for p in expired:
        _dead_proxies.pop(p, None)


def mark_proxy_dead(proxy: str, ttl: float = None) -> None:
    if not proxy:
        return
    expire_at = time.time() + float(ttl or UPLOAD_DEAD_TTL)
    with _dead_lock:
        _dead_proxies[proxy] = expire_at


def get_fresh_proxy(exclude: set = None) -> "str | None":
    exclude = exclude or set()
    _load_pool()
    with _dead_lock:
        _cleanup_expired_dead()
        dead = set(_dead_proxies.keys())
    with _lock:
        candidates = [p for p in _cached_proxies if p not in dead and p not in exclude]
    if not candidates:
        return None
    return random.choice(candidates)


def is_pool_exhausted() -> bool:
    _load_pool()
    with _dead_lock:
        _cleanup_expired_dead()
        dead = set(_dead_proxies.keys())
    with _lock:
        return not any(p not in dead for p in _cached_proxies)


def dead_count() -> int:
    with _dead_lock:
        _cleanup_expired_dead()
        return len(_dead_proxies)
