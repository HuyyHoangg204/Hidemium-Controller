"""
lane_manager.py — Per-account lane serialisation + error recovery.

Derived from Banana Python Integration docs §05 (Concurrency And Lane Model).

Core invariants:
  - Each account = 1 lane
  - Each lane allows at most 1 create API call at a time (serialised via Lock)
  - Error recovery is deterministic: classify_error() → handle_error()
  - Upload/poll/download can run concurrently (not serialised)

Usage:
    from core.lane_manager import LaneManager

    # In VeoService.__init__:
    self._lane_manager = LaneManager()

    # In _run_create_image / _run_t2v / _run_i2v:
    lane = self._lane_manager.get_lane(account_name)
    result = lane.execute_create(api_call_fn, *args)

    # Error recovery:
    from core.error_table import classify_error
    action, meta = classify_error(api_detail, http_status)
    should_retry = lane.handle_error(action, meta, veo_service_instance, account_name)
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

from .error_table import ErrorAction, classify_error

logger = logging.getLogger(__name__)


class AccountLane:
    """
    One account = one lane.

    Serialises create API calls (generate_image, create_video_t2v, etc.)
    via a threading.Lock. Upload/poll/download are NOT serialised.

    Tracks consecutive 403 count for threshold-based lane reset (doc §06).
    """

    # Max consecutive 403 before triggering full lane reset (F5 + proxy rotate)
    MAX_CONSECUTIVE_403 = 3

    def __init__(self, account_name: str):
        self.account_name = account_name
        self._create_lock = threading.Lock()

        # Lane state (doc §07 LaneState)
        self.healthy = True
        self.active_creates = 0
        self.consecutive_403 = 0
        self.last_error: Optional[str] = None
        self._last_success_time: float = 0

    # ── Serialised create ────────────────────────────────────────────────

    def execute_create(self, fn: Callable, *args, **kwargs) -> Any:
        """
        Execute a create API call with serialisation.

        Only 1 thread can call create API per account at a time.
        This prevents concurrent create calls from the same account
        (which causes Google to flag as UNUSUAL_ACTIVITY).

        Args:
            fn: The API call function (e.g. img_client.generate_image)
            *args, **kwargs: Arguments to pass to fn

        Returns:
            Whatever fn returns
        """
        with self._create_lock:
            self.active_creates += 1
            try:
                logger.info(
                    "[Lane] %s: CREATE start (active=%d, healthy=%s)",
                    self.account_name, self.active_creates, self.healthy,
                )
                result = fn(*args, **kwargs)

                # Success → reset 403 counter
                if result is not None:
                    self.consecutive_403 = 0
                    self._last_success_time = time.time()
                    self.last_error = None

                return result
            finally:
                self.active_creates -= 1

    # ── Error recovery ───────────────────────────────────────────────────

    def handle_error(
        self,
        action: ErrorAction,
        meta: dict,
        service: Any,  # VeoService instance
        account_name: str,
    ) -> bool:
        """
        Execute error recovery action. Returns True to retry, False to fail.

        This is the single point of error recovery logic, replacing the
        200+ LOC inline if/elif chains in each flow method.

        Args:
            action: ErrorAction from classify_error()
            meta: metadata dict from classify_error()
            service: VeoService instance (for F5, proxy rotation, etc.)
            account_name: account label for logging

        Returns:
            True = retry the operation, False = fail the task
        """
        reason = meta.get("reason", "unknown")
        sleep_s = meta.get("sleep", 1)

        logger.info(
            "[Lane] %s: handle_error action=%s reason=%s sleep=%s",
            account_name, action.value, reason, sleep_s,
        )

        if action == ErrorAction.FAIL_PERMANENT:
            self.last_error = reason
            logger.warning(
                "[Lane] %s: PERMANENT FAIL — %s",
                account_name, reason,
            )
            return False

        if action == ErrorAction.F5_RETRY:
            # F5 tab + optional proxy rotation + sleep
            if meta.get("rotate_proxy") == "static":
                self._rotate_static_proxy(service, account_name)

            # Track 403 counter
            if "403" in str(meta.get("http_status", "")):
                self.consecutive_403 += 1
                if self.consecutive_403 >= self.MAX_CONSECUTIVE_403:
                    logger.warning(
                        "[Lane] %s: 403 threshold reached (%d) — full reset",
                        account_name, self.consecutive_403,
                    )
                    self.consecutive_403 = 0

            # F5 via bridge
            try:
                service._trigger_f5(account_name, reason=reason, sleep_s=sleep_s)
            except Exception as e:
                logger.warning("[Lane] %s: F5 failed: %s — sleeping %ss", account_name, e, sleep_s)
                time.sleep(sleep_s)
            return True

        if action == ErrorAction.ROTATE_PROXY_RETRY:
            self._rotate_proxy(service, account_name, meta)
            time.sleep(sleep_s)
            return True

        if action == ErrorAction.RELOAD_COOKIE_RETRY:
            self._reload_cookie(service, account_name)
            if meta.get("rotate_proxy"):
                self._rotate_static_proxy(service, account_name)
            time.sleep(sleep_s)
            return True

        if action == ErrorAction.BACKOFF_RETRY:
            logger.info(
                "[Lane] %s: backoff %ss (reason=%s)",
                account_name, sleep_s, reason,
            )
            # F5 first if available
            try:
                service._trigger_f5(account_name, reason=reason, sleep_s=sleep_s)
            except Exception:
                time.sleep(sleep_s)
            return True

        if action == ErrorAction.RETRY_SAME:
            time.sleep(sleep_s)
            return True

        # Unknown action → retry with short sleep
        time.sleep(1)
        return True

    # ── Private helpers ──────────────────────────────────────────────────

    @staticmethod
    def _rotate_static_proxy(service: Any, account_name: str):
        """Rotate to a fresh static proxy."""
        try:
            from core.static_proxy_pool import get_random_proxy
            new_ip = get_random_proxy()
            if new_ip:
                logger.info(
                    "[Lane] %s: rotated to static proxy %s",
                    account_name, new_ip[:50],
                )
                return new_ip
        except Exception as e:
            logger.warning("[Lane] %s: static proxy rotation failed: %s", account_name, e)
        return None

    @staticmethod
    def _rotate_proxy(service: Any, account_name: str, meta: dict):
        """Rotate proxy: try KiotProxy first, fallback to static."""
        new_ip = None
        try:
            new_ip = service._rotate_kiotproxy_key_for_account(account_name)
        except Exception:
            pass
        if not new_ip:
            try:
                from core.static_proxy_pool import get_random_proxy
                new_ip = get_random_proxy()
            except Exception:
                pass
        if new_ip:
            logger.info(
                "[Lane] %s: proxy rotated → %s (reason=%s)",
                account_name, new_ip[:50], meta.get("reason"),
            )
        else:
            logger.warning(
                "[Lane] %s: proxy rotation FAILED (reason=%s)",
                account_name, meta.get("reason"),
            )
        return new_ip

    @staticmethod
    def _reload_cookie(service: Any, account_name: str):
        """Reload cookie from DB (extension may have pushed a new token)."""
        try:
            from web.store import store
            fresh_acc = store.get_veo_account_by_name(account_name)
            if fresh_acc and getattr(fresh_acc, "cookie", None):
                logger.info(
                    "[Lane] %s: cookie reloaded from DB",
                    account_name,
                )
                return fresh_acc
        except Exception as e:
            logger.warning("[Lane] %s: cookie reload failed: %s", account_name, e)
        return None


class LaneManager:
    """
    Global registry of account lanes.

    Thread-safe: get_lane() uses a lock to create lanes on demand.
    Each account gets exactly one lane (singleton per account name).
    """

    def __init__(self):
        self._lanes: dict[str, AccountLane] = {}
        self._lock = threading.Lock()

    def get_lane(self, account_name: str) -> AccountLane:
        """Get or create a lane for the given account (thread-safe)."""
        with self._lock:
            lane = self._lanes.get(account_name)
            if lane is None:
                lane = AccountLane(account_name)
                self._lanes[account_name] = lane
                logger.info(
                    "[LaneManager] Created lane for %s (total=%d)",
                    account_name, len(self._lanes),
                )
            return lane

    def get_all_lanes(self) -> list[AccountLane]:
        """Return all registered lanes."""
        with self._lock:
            return list(self._lanes.values())

    def pick_healthy_lane(self) -> Optional[AccountLane]:
        """Pick the healthy lane with fewest active creates."""
        with self._lock:
            healthy = [l for l in self._lanes.values() if l.healthy]
            if not healthy:
                return None
            return min(healthy, key=lambda l: l.active_creates)

    def reset_lane(self, account_name: str):
        """Reset a lane's state (after successful recovery)."""
        with self._lock:
            lane = self._lanes.get(account_name)
            if lane:
                lane.consecutive_403 = 0
                lane.healthy = True
                lane.last_error = None
                logger.info("[LaneManager] Lane %s reset", account_name)
