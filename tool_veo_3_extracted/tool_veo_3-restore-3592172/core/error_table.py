"""
error_table.py — Centralized error classification for Google Labs API.

Derived from Banana Python Integration docs §06 (Error Handling And Recovery).
Single source of truth: ALL flows (CreateImage, T2V, I2V, I2V-B64) call
``classify_error()`` instead of inline if/elif chains.

Usage:
    from core.error_table import classify_error, ErrorAction
    action, meta = classify_error(api_detail_str, http_status_code)

Each call returns exactly ONE ``ErrorAction`` enum + a ``dict`` of metadata
(sleep seconds, whether to rotate proxy, etc.).
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Optional, Tuple, Dict, Any

import logging

logger = logging.getLogger(__name__)


# ─── Error Actions ───────────────────────────────────────────────────────────

class ErrorAction(Enum):
    """Deterministic recovery action for a classified error."""

    # Retry immediately on same account + same proxy
    RETRY_SAME = "retry_same"

    # F5 (reload) browser tab + retry same account
    F5_RETRY = "f5_retry"

    # Rotate to a different proxy IP + retry same account
    ROTATE_PROXY_RETRY = "rotate_proxy_retry"

    # Reload cookie from DB (session expired) + retry same account
    RELOAD_COOKIE_RETRY = "reload_cookie_retry"

    # Backoff: sleep N seconds then retry same account
    BACKOFF_RETRY = "backoff_retry"

    # Fail task permanently — content error, do NOT retry
    FAIL_PERMANENT = "fail_permanent"


# ─── Error Patterns (ordered by priority — first match wins) ────────────────

# Each entry: (pattern_list, http_status_match, action, default_metadata)
# pattern_list: list of strings to match in api_detail (case-insensitive)
# http_status_match: exact HTTP status to match, or None for any
# action: ErrorAction enum
# default_metadata: dict with keys like sleep, rotate_proxy, reason

_ERROR_RULES: list[tuple[list[str], Optional[int], ErrorAction, dict]] = [
    # ── 400 UNSAFE — permanent fail (doc §06: do NOT retry) ──
    (
        ["UNSAFE_GENERATION", "UNSAFE"],
        400,
        ErrorAction.FAIL_PERMANENT,
        {"reason": "prompt_unsafe", "sleep": 0},
    ),

    # ── 401 Auth / Cookie expired — reload cookie + rotate proxy ──
    (
        ["401", "auth variants failed", "Cookie"],
        None,
        ErrorAction.RELOAD_COOKIE_RETRY,
        {"reason": "auth_expired", "sleep": 2, "rotate_proxy": "static"},
    ),

    # ── 403 UNUSUAL_ACTIVITY — F5 + static proxy + retry ──
    (
        ["UNUSUAL_ACTIVITY"],
        None,
        ErrorAction.F5_RETRY,
        {"reason": "unusual_activity", "sleep": 10, "rotate_proxy": "static"},
    ),

    # ── 403 reCAPTCHA evaluation failed — F5 + retry captcha ──
    (
        ["reCAPTCHA evaluation failed", "reCAPTCHA"],
        None,
        ErrorAction.F5_RETRY,
        {"reason": "recaptcha_failed", "sleep": 10, "rotate_proxy": "static",
         "refresh_captcha": True},
    ),

    # ── TOO_MUCH_TRAFFIC — F5 + retry (doc §06: F5-recoverable) ──
    (
        ["TOO_MUCH_TRAFFIC"],
        None,
        ErrorAction.F5_RETRY,
        {"reason": "too_much_traffic", "sleep": 5},
    ),

    # ── 429 / RESOURCE_EXHAUSTED — backoff then retry ──
    (
        ["429", "RESOURCE_EXHAUSTED"],
        None,
        ErrorAction.BACKOFF_RETRY,
        {"reason": "rate_limited", "sleep": 5},
    ),

    # ── DAILY_QUOTA — longer backoff ──
    (
        ["DAILY_QUOTA", "PER_MODEL_DAILY_QUOTA"],
        None,
        ErrorAction.BACKOFF_RETRY,
        {"reason": "daily_quota", "sleep": 30},
    ),

    # ── Proxy dead / IP filter — rotate proxy ──
    (
        ["PROXY_DEAD", "IP_FILTER", "IP_INPUT", "10061", "refused",
         "ProxyError", "Unable to connect to proxy",
         "Failed to establish a new connection",
         "actively refused", "WinError 10061",
         "ProxyConnectionError"],
        None,
        ErrorAction.ROTATE_PROXY_RETRY,
        {"reason": "proxy_dead", "sleep": 1},
    ),

    # ── 5xx server errors — F5 + retry (doc §06) ──
    (
        ["500", "502", "503", "504",
         "DEADLINE_EXCEEDED", "deadline exceeded",
         "internal server error", "internal_server_error",
         "service unavailable", "service_unavailable",
         "backend error", "backend_error"],
        None,
        ErrorAction.F5_RETRY,
        {"reason": "server_error", "sleep": 5},
    ),

    # ── Execution context / browser death — F5 + retry ──
    (
        ["execution context was destroyed", "target closed",
         "page closed", "context closed", "browser has been closed",
         "connection closed"],
        None,
        ErrorAction.F5_RETRY,
        {"reason": "browser_death", "sleep": 3},
    ),
]


def classify_error(
    api_detail: str,
    http_status: Optional[int] = None,
) -> Tuple[ErrorAction, Dict[str, Any]]:
    """Classify an API error into a deterministic action.

    Args:
        api_detail: Error detail string from client._last_error_detail or
                    exception message. Case-insensitive matching.
        http_status: HTTP status code if available (e.g. 403, 429).

    Returns:
        (ErrorAction, metadata_dict) — metadata contains:
            - reason: str — human-readable error category
            - sleep: float — seconds to sleep before retry
            - rotate_proxy: Optional[str] — "static" or "kiot" if should rotate
            - refresh_captcha: bool — True if should get new captcha token
    """
    detail_lower = (api_detail or "").lower()

    # Also try to extract HTTP status from the detail string if not provided
    if http_status is None:
        # Try to find "status=NNN" or "HTTP NNN" or just "NNN" patterns
        m = re.search(r'(?:status[=:]\s*|HTTP\s+)(\d{3})', api_detail or "")
        if m:
            try:
                http_status = int(m.group(1))
            except ValueError:
                pass

    for patterns, status_match, action, meta_template in _ERROR_RULES:
        # Check HTTP status match (if rule specifies one)
        if status_match is not None:
            if http_status != status_match:
                # Also check if status appears in detail string
                if str(status_match) not in (api_detail or ""):
                    continue

        # Check pattern match (any pattern in the list)
        matched = False
        for pattern in patterns:
            if pattern.lower() in detail_lower:
                matched = True
                break

        if matched:
            # Build metadata (copy template + add extracted info)
            meta = dict(meta_template)
            meta["http_status"] = http_status
            meta["matched_pattern"] = pattern if matched else None
            logger.debug(
                "[ErrorTable] Classified: action=%s reason=%s pattern=%s status=%s",
                action.value, meta.get("reason"), pattern, http_status,
            )
            return action, meta

    # ── Default: generic retry (same account, short sleep) ──
    meta = {
        "reason": "unknown",
        "sleep": 1,
        "http_status": http_status,
    }
    logger.debug(
        "[ErrorTable] Unclassified error → RETRY_SAME: %s",
        (api_detail or "")[:120],
    )
    return ErrorAction.RETRY_SAME, meta


def is_f5_recoverable(api_detail: str = None, http_status: int = None) -> bool:
    """Backward-compatible check: True if error is F5-recoverable.

    Drop-in replacement for ``VeoService._is_f5_recoverable_error()``.
    """
    action, _ = classify_error(api_detail or "", http_status)
    return action in (ErrorAction.F5_RETRY, ErrorAction.BACKOFF_RETRY)
