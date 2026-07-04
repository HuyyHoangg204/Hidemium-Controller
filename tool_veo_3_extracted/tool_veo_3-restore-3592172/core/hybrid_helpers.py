"""
hybrid_helpers.py — Helper dùng chung cho HybridImagenClient & HybridVeoClient.

`call_with_bridge_priority`: ưu tiên gọi qua browser bridge tối đa `max_wait`
giây. Nếu trong khoảng đó bridge OFFLINE → sleep+poll. Bridge ONLINE và call
thành công → return ngay. Hết deadline → fallback httpx.

Mục đích: khi Chrome tạm offline (extension reload / bridge restart) thì task
KHÔNG fallback xuống httpx ngay (httpx hay dính UNUSUAL_ACTIVITY) mà đợi
bridge phục hồi → bridge "ưu tiên tối đa" như user yêu cầu.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Thời gian tối đa đợi bridge online trước khi fallback httpx (giây).
# Giảm mặc định từ 60s xuống 2s để pre-upload không làm nghẽn queue khi bridge offline.
try:
    DEFAULT_BRIDGE_WAIT = float(os.getenv("VEO_BRIDGE_UPLOAD_WAIT_SECONDS", "2"))
except Exception:
    DEFAULT_BRIDGE_WAIT = 2.0
# Khoảng poll bridge khi đang offline (giây).
DEFAULT_POLL_INTERVAL = 0.5


def call_with_bridge_priority(
    *,
    is_browser_available: Callable[[], bool],
    browser_fn: Callable[[], Any],
    httpx_fn: Callable[[], Any],
    label: str = "",
    max_wait: float = DEFAULT_BRIDGE_WAIT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> Any:
    """Loop đợi bridge online → call browser_fn. Hết max_wait → fallback httpx.

    Quy ước:
        browser_fn() → trả result truthy nếu OK, None/empty nếu fail (raise
            cũng coi là fail; sẽ retry trong vòng đợi).
        httpx_fn()   → fallback cuối cùng, gọi đúng 1 lần khi bridge timeout.
    """
    deadline = time.time() + max_wait
    last_err: str | None = None
    bridge_was_offline_logged = False
    while time.time() < deadline:
        if is_browser_available():
            try:
                res = browser_fn()
                if res:
                    return res
                last_err = "empty result"
                logger.warning(
                    f"[Hybrid] {label} bridge trả empty/None "
                    f"(còn {deadline - time.time():.0f}s) — retry"
                )
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                logger.warning(
                    f"[Hybrid] {label} bridge exception "
                    f"(còn {deadline - time.time():.0f}s): {last_err}"
                )
        else:
            if not bridge_was_offline_logged:
                logger.info(
                    f"[Hybrid] {label} bridge OFFLINE — đợi tối đa "
                    f"{max_wait:.0f}s rồi mới fallback httpx"
                )
                bridge_was_offline_logged = True
        time.sleep(poll_interval)

    logger.warning(
        f"[Hybrid] {label} bridge ưu tiên hết {max_wait:.0f}s "
        f"(last_err={last_err}) → fallback httpx"
    )
    return httpx_fn()
