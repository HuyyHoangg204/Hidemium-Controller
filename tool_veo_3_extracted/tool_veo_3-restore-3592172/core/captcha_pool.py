"""
captcha_pool.py — Kho token reCAPTCHA chung (prefetch pool).

Architecture:
  ┌─ Producer Extension (1 điều phối thread + ThreadPoolExecutor batch) ─┐
  │    submit batch = min(need, workers_online, EXTENSION_BATCH_MAX)      │
  │                                                                        ├──→ [ KHO TOKEN ]  ←── Task lấy token
  └─ Producer Remote (1 thread sequential, throughput ~6s/token)  ────────┘         │
                                                                                     ├─ Cleaner thread purge token gần hết hạn
                                                                                     ├─ Sub-pool theo action: IMAGE_GENERATION / VIDEO_GENERATION
                                                                                     └─ get_token(): pool hit → trả ngay; miss → on-demand fallback

reCAPTCHA token life cycle (xác nhận từ Google docs):
  - Sống đúng 120s từ lúc giải ra.
  - Single-use — Google sẽ reject "timeout-or-duplicate" nếu dùng lại.

Pool config:
  - TOKEN_TTL = 100s (margin 20s vs 120s của Google).
  - MIN_REMAINING_AT_CONSUME = 20s — token cấp ra còn ≥ 20s sống → đủ budget cho upload + POST.
  - TARGET_POOL_SIZE = 4 token / sub-pool — đủ buffer mà không stockpile token cũ.
  - Extension batch up to EXTENSION_BATCH_MAX song song để khai thác nhiều Chrome worker.
  - Remote sequential 1 producer thread (server có rate-limit, không nên flood).
  - Producer fail liên tục → backoff tăng dần (max 30s).
"""

import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────
TOKEN_TTL = 100                  # giây — Google cho 120s, lưu 100s để có margin
MIN_REMAINING_AT_CONSUME = 20    # giây — token cấp ra còn ≥ 20s sống
TARGET_POOL_SIZE = 16            # token đích cho mỗi sub-pool (per-action)

# Extension batch (khai thác N Chrome worker song song)
EXTENSION_BATCH_MAX = 20         # số job Extension tối đa submit song song / vòng
EXTENSION_BACKOFF_NO_WORKER = 30 # khi không có worker online → sleep
EXTENSION_BACKOFF_FAIL = 5       # khi cả batch fail → sleep

# Remote (1 producer sequential)
REMOTE_BACKOFF_OK = 0.5
REMOTE_BACKOFF_FAIL_BASE = 5
REMOTE_BACKOFF_FAIL_MAX = 30

# Common
PRODUCER_BACKOFF_FULL = 2        # nghỉ khi pool đã đầy

# Get-token: chờ pool tối đa bao lâu trước khi fallback on-demand
POOL_WAIT_BEFORE_FALLBACK = 5

# Cleaner
CLEANER_INTERVAL = 15            # giây giữa 2 lần purge

ACTIONS = ("IMAGE_GENERATION", "VIDEO_GENERATION")


class TokenEntry:
    """Một token trong kho."""

    __slots__ = ("token", "source", "action", "created_at", "use_count")

    def __init__(self, token: str, source: str, action: str):
        self.token = token
        self.source = source
        self.action = action
        self.created_at = time.time()
        self.use_count = 0

    @property
    def expired(self) -> bool:
        return time.time() - self.created_at > TOKEN_TTL

    @property
    def age(self) -> int:
        return int(time.time() - self.created_at)

    @property
    def remaining(self) -> int:
        return max(0, TOKEN_TTL - int(time.time() - self.created_at))

    def __repr__(self):
        return (
            f"Token({self.source}/{self.action}, "
            f"age={self.age}s, rem={self.remaining}s)"
        )


class _SubPool:
    """Sub-pool cho 1 action — deque thread-safe + counters."""

    def __init__(self, action: str):
        self.action = action
        self._deque: "deque[TokenEntry]" = deque()
        self._lock = threading.Lock()
        self.stats = {
            "produced": 0,
            "consumed_pool": 0,
            "consumed_ondemand": 0,
            "expired": 0,
            "produce_fail": 0,
        }

    def push(self, entry: TokenEntry):
        with self._lock:
            self._deque.append(entry)
            self.stats["produced"] += 1

    def pop_fresh(self, min_remaining: int = MIN_REMAINING_AT_CONSUME) -> Optional[TokenEntry]:
        """Pop token đầu tiên còn ≥ min_remaining giây sống. Bỏ qua các token gần chết."""
        with self._lock:
            while self._deque:
                entry = self._deque.popleft()
                if entry.remaining >= min_remaining:
                    self.stats["consumed_pool"] += 1
                    return entry
                self.stats["expired"] += 1
            return None

    def purge_expired(self) -> int:
        with self._lock:
            keep = deque()
            removed = 0
            for entry in self._deque:
                if entry.remaining >= MIN_REMAINING_AT_CONSUME:
                    keep.append(entry)
                else:
                    removed += 1
            self._deque = keep
            self.stats["expired"] += removed
            return removed

    def record_fail(self):
        with self._lock:
            self.stats["produce_fail"] += 1

    def record_ondemand(self):
        with self._lock:
            self.stats["consumed_ondemand"] += 1

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._deque)


class CaptchaPool:
    """Prefetch reCAPTCHA token pool (Google TTL 120s — ta lưu 100s)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._running = False
        self._sub_pools: dict[str, _SubPool] = {a: _SubPool(a) for a in ACTIONS}
        self._active_action = "IMAGE_GENERATION"
        self._producer_threads: list[threading.Thread] = []
        self._cleaner_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        # Executor cho Extension batch — spawn lazy trong start()
        self._ext_executor: Optional[ThreadPoolExecutor] = None

    # ── Lifecycle ───────────────────────────────────────

    def start(self, action: str = "IMAGE_GENERATION"):
        """Khởi động producer threads (idempotent — gọi lại chỉ đổi active action)."""
        with self._lock:
            self._active_action = action
            if self._running:
                return
            self._running = True
            self._stop_event.clear()

            # Executor cho Extension batch
            self._ext_executor = ThreadPoolExecutor(
                max_workers=EXTENSION_BATCH_MAX,
                thread_name_prefix="ExtSolver",
            )

            # 1 thread điều phối Extension (batch parallel)
            t_ext = threading.Thread(
                target=self._extension_producer_loop,
                name="CaptchaProducer-Extension",
                daemon=True,
            )
            t_ext.start()
            self._producer_threads.append(t_ext)

            # 1 thread sequential Remote
            t_rem = threading.Thread(
                target=self._remote_producer_loop,
                name="CaptchaProducer-Remote",
                daemon=True,
            )
            t_rem.start()
            self._producer_threads.append(t_rem)

            self._cleaner_thread = threading.Thread(
                target=self._cleaner_loop,
                name="CaptchaCleaner",
                daemon=True,
            )
            self._cleaner_thread.start()

        logger.info(
            f"[CaptchaPool] ✅ Started prefetch — active_action={action}, "
            f"target={TARGET_POOL_SIZE}/action, ttl={TOKEN_TTL}s, "
            f"ext_batch_max={EXTENSION_BATCH_MAX}"
        )

    def stop(self):
        with self._lock:
            self._running = False
            self._stop_event.set()
            if self._ext_executor:
                # Không chờ pending — daemon thread sẽ exit
                self._ext_executor.shutdown(wait=False, cancel_futures=True)
                self._ext_executor = None
        logger.info("[CaptchaPool] Stopped (producers will exit)")

    def set_action(self, action: str):
        """Đổi active action — producer sẽ bơm vào sub-pool mới."""
        with self._lock:
            if action != self._active_action:
                logger.info(
                    f"[CaptchaPool] active action: {self._active_action} → {action}"
                )
                self._active_action = action

    # ── Pool info ───────────────────────────────────────

    @property
    def size(self) -> int:
        """Số token trong sub-pool của active action."""
        return self._sub_pools[self._active_action].size

    @property
    def running(self) -> bool:
        return self._running

    def stats_snapshot(self) -> dict:
        return {
            a: {"size": p.size, **dict(p.stats)}
            for a, p in self._sub_pools.items()
        }

    # ── Consumer API ────────────────────────────────────

    def get_token(self, timeout: float = 60, action: str = None) -> Optional[TokenEntry]:
        """
        Lấy 1 token: ưu tiên pop từ kho, miss thì on-demand fallback.

        Behaviour:
          - Wait tối đa POOL_WAIT_BEFORE_FALLBACK (5s) cho producer bơm vào kho.
          - Nếu vẫn rỗng → fallback on-demand (chiếm caller thread như chế độ cũ).
          - Token cấp ra còn ≥ MIN_REMAINING_AT_CONSUME (10s) sống.

        Returns:
            TokenEntry hoặc None nếu cả pool lẫn on-demand đều fail.
        """
        action = action or self._active_action
        sub = self._sub_pools.get(action)
        if not sub:
            logger.warning(f"[CaptchaPool] Unknown action: {action}")
            return None

        # Đảm bảo producer đang bơm cho action này
        self.set_action(action)

        # 1) Thử pop từ pool
        deadline = time.time() + min(timeout, POOL_WAIT_BEFORE_FALLBACK)
        while time.time() < deadline:
            entry = sub.pop_fresh()
            if entry:
                logger.info(
                    f"[CaptchaPool] 🎯 Pool hit: {entry.source}/{action} "
                    f"rem={entry.remaining}s, pool_size_after={sub.size}"
                )
                entry.use_count = 1
                return entry
            self._stop_event.wait(0.2)

        # 2) Pool miss → fallback on-demand
        logger.info(
            f"[CaptchaPool] Pool empty for {action} after "
            f"{POOL_WAIT_BEFORE_FALLBACK}s → on-demand fallback"
        )
        remaining_timeout = max(timeout - POOL_WAIT_BEFORE_FALLBACK, 10)
        return self._solve_on_demand(action, remaining_timeout)

    def report_result(self, entry: TokenEntry, passed: bool):
        """
        Báo kết quả sau khi dùng token.

        reCAPTCHA token là **single-use**: dù pass hay fail đều không đưa lại pool.
        Hàm này chỉ để log + future stats.
        """
        if not entry:
            return
        if passed:
            logger.info(
                f"[CaptchaPool] ✅ {entry.source}/{entry.action} PASS (single-use, discarded)"
            )
        else:
            logger.info(
                f"[CaptchaPool] ❌ {entry.source}/{entry.action} FAIL"
            )

    # ── Producer / Cleaner ──────────────────────────────

    def _extension_producer_loop(self):
        """
        Điều phối Extension producer — submit batch song song để khai thác
        nhiều Chrome worker cùng lúc.

        Mỗi vòng:
          1. Tính need = TARGET - sub.size
          2. workers_online = số Chrome đang kết nối
          3. batch = min(need, workers_online, EXTENSION_BATCH_MAX)
          4. Submit batch job vào _ext_executor → đợi tất cả về → push token vào pool
        """
        logger.info("[CaptchaPool] Producer Extension (batch) started")
        try:
            from core.captcha_server import get_connected_accounts
        except Exception:
            logger.error("[CaptchaPool] Extension: import captcha_server failed, exit producer")
            return

        no_worker_logged = False

        while not self._stop_event.is_set():
            action = self._active_action
            sub = self._sub_pools[action]

            # Pool đã đầy → ngủ ngắn
            need = TARGET_POOL_SIZE - sub.size
            if need <= 0:
                self._stop_event.wait(PRODUCER_BACKOFF_FULL)
                continue

            try:
                workers = get_connected_accounts()
            except Exception:
                workers = []
            n = len(workers)

            if n == 0:
                if not no_worker_logged:
                    logger.info(
                        f"[CaptchaPool] Extension: no Chrome worker online → "
                        f"sleep {EXTENSION_BACKOFF_NO_WORKER}s"
                    )
                    no_worker_logged = True
                self._stop_event.wait(EXTENSION_BACKOFF_NO_WORKER)
                continue
            if no_worker_logged:
                logger.info(f"[CaptchaPool] Extension: {n} Chrome worker(s) back online")
                no_worker_logged = False

            batch = min(need, n, EXTENSION_BATCH_MAX)
            logger.info(
                f"[CaptchaPool] Extension batch: submit {batch} jobs "
                f"(need={need}, workers={n}, action={action})"
            )

            executor = self._ext_executor
            if not executor:
                # stop() đã được gọi
                break

            t_start = time.time()
            futures = [
                executor.submit(self._solve_one, "Extension", action)
                for _ in range(batch)
            ]
            ok = 0
            fail = 0
            for f in as_completed(futures):
                try:
                    token = f.result()
                except Exception as e:
                    logger.error(f"[CaptchaPool] Extension batch worker crash: {e}")
                    token = None
                if token:
                    sub.push(TokenEntry(token, "Extension", action))
                    ok += 1
                else:
                    sub.record_fail()
                    fail += 1

            elapsed = round(time.time() - t_start, 1)
            logger.info(
                f"[CaptchaPool] Extension batch done in {elapsed}s: "
                f"+{ok} ok, {fail} fail → pool_size={sub.size}"
            )

            if ok == 0 and fail > 0:
                # Cả batch fail → backoff dài
                self._stop_event.wait(EXTENSION_BACKOFF_FAIL)

        logger.info("[CaptchaPool] Producer Extension stopped")

    def _remote_producer_loop(self):
        """1 producer thread sequential cho Remote (server có rate-limit)."""
        logger.info("[CaptchaPool] Producer Remote started")
        consecutive_fail = 0

        while not self._stop_event.is_set():
            action = self._active_action
            sub = self._sub_pools[action]

            # Pool đã đầy → ngủ ngắn
            if sub.size >= TARGET_POOL_SIZE:
                self._stop_event.wait(PRODUCER_BACKOFF_FULL)
                continue

            try:
                token = self._solve_one("Remote", action)
            except Exception as e:
                logger.error(f"[CaptchaPool] Producer Remote crash: {e}")
                token = None

            if token:
                sub.push(TokenEntry(token, "Remote", action))
                consecutive_fail = 0
                logger.info(
                    f"[CaptchaPool] +1 via Remote action={action} → pool_size={sub.size}"
                )
                self._stop_event.wait(REMOTE_BACKOFF_OK)
            else:
                consecutive_fail += 1
                sub.record_fail()
                wait = min(
                    REMOTE_BACKOFF_FAIL_BASE * consecutive_fail,
                    REMOTE_BACKOFF_FAIL_MAX,
                )
                if consecutive_fail >= 3:
                    logger.warning(
                        f"[CaptchaPool] ⚠ Producer Remote failed {consecutive_fail}x "
                        f"in a row — backing off {wait}s"
                    )
                self._stop_event.wait(wait)

        logger.info("[CaptchaPool] Producer Remote stopped")

    def _cleaner_loop(self):
        """Mỗi CLEANER_INTERVAL giây: purge token gần hết hạn ở mọi sub-pool."""
        while not self._stop_event.is_set():
            self._stop_event.wait(CLEANER_INTERVAL)
            if self._stop_event.is_set():
                break
            for action, sub in self._sub_pools.items():
                removed = sub.purge_expired()
                if removed:
                    logger.info(
                        f"[CaptchaPool] 🧹 Purged {removed} stale token(s) "
                        f"from {action} → pool_size={sub.size}"
                    )

    def _solve_on_demand(self, action: str, timeout: float) -> Optional[TokenEntry]:
        """Backward-compat fallback khi pool rỗng — giải ngay trên caller thread."""
        sub = self._sub_pools[action]
        started = time.time()
        token = None
        source_used = None

        try:
            from core.captcha_server import get_connected_accounts
            if get_connected_accounts():
                source_used = "Extension"
                token = self._solve_one("Extension", action)
        except Exception:
            pass

        if not token:
            source_used = "Remote"
            token = self._solve_one("Remote", action)

        if not token:
            elapsed = int(time.time() - started)
            logger.warning(
                f"[CaptchaPool] ⏰ on-demand failed action={action} (took {elapsed}s)"
            )
            return None

        entry = TokenEntry(token, source_used, action)
        entry.use_count = 1
        sub.record_ondemand()
        logger.info(
            f"[CaptchaPool] 🎯 On-demand token: {entry.source}/{action} "
            f"(took={int(time.time()-started)}s)"
        )
        return entry

    def _solve_one(self, source: str, action: str) -> Optional[str]:
        """Giải 1 captcha từ nguồn chỉ định (blocking)."""
        if source == "Extension":
            try:
                from core.captcha_server import request_captcha, get_connected_accounts
                if not get_connected_accounts():
                    return None
                return request_captcha(action=action, timeout=15)
            except Exception:
                return None

        if source == "Remote":
            try:
                from core.remote_captcha import request_remote_captcha
                return request_remote_captcha(action=action, timeout=45)
            except Exception:
                return None

        return None


# ── Singleton ───────────────────────────────────────────
_pool_instance: Optional[CaptchaPool] = None
_pool_lock = threading.Lock()


def get_pool() -> CaptchaPool:
    """Lấy singleton CaptchaPool (tạo nếu chưa có)."""
    global _pool_instance
    if _pool_instance is None:
        with _pool_lock:
            if _pool_instance is None:
                _pool_instance = CaptchaPool()
    return _pool_instance
