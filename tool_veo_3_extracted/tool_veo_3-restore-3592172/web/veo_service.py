import os
import sys
import uuid
import time
import threading
import logging
import json
import urllib.error
import urllib.request
import datetime
import hashlib
from typing import Optional, List

from web.models import (
    VideoTask,
    TaskStatus,
    ActionType,
    resolve_model_key,
    VeoModel,
)
from web.store import store
from web.exceptions import TaskNotFoundError, ValidationError

# Module-level import để _run_i2v / _run_i2v_b64 (chạy trong worker thread) có VeoClient.
# Trước đây chỉ import cục bộ trong _resolve_client_for_task → line 2981/3318 bắn NameError.
from core.veo_client import VeoClient  # noqa: E402
import concurrent.futures as _futures

# ── Banana approach: centralized error classification + lane serialisation ──
from core.error_table import classify_error, ErrorAction, is_f5_recoverable
from core.lane_manager import LaneManager

logger = logging.getLogger(__name__)


def _veo_browser_runtime_enabled() -> bool:
    """True khi video create/upscale tự resolve reCAPTCHA trong VPS Chrome runtime."""
    try:
        from core import browser_config as _bcfg
        return bool(_bcfg.get("veo_browser_runtime_enabled", True))
    except Exception:
        return True


# ── Direct file log: bypasses logging framework entirely ──
# Dùng khi cần đảm bảo 100% log vào file kể cả khi propagation lỗi
_LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "veo_api.log"
)


def _filelog(msg: str):
    """Ghi trực tiếp vào log file bằng raw file I/O — không qua stdout/logging."""
    try:
        os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(_LOG_PATH, "a", encoding="utf-8", errors="replace") as _f:
            _f.write(f"{ts} [VSVC] {msg}\n")
    except Exception:
        pass


def _reference_credit_token_key(token: str, length: int = 12) -> str:
    return hashlib.sha1(str(token or "").encode("utf-8", errors="ignore")).hexdigest()[:length]


def _reference_credit_normalize_proxy(proxy: str | None) -> str:
    value = str(proxy or "").strip()
    if not value:
        return ""
    if "://" in value:
        return value
    parts = value.split(":")
    if len(parts) == 4 and all(parts):
        host, port, user, password = parts
        return f"http://{user}:{password}@{host}:{port}"
    return f"http://{value}"


def _reference_check_token_credit_local(token: str, proxy: str = "", project_id: str = "", timeout: int = 20) -> dict:
    token = str(token or "").strip()
    proxy = str(proxy or "").strip()
    project_id = str(project_id or "").strip()
    token_key = _reference_credit_token_key(token)
    result = {"ok": False, "token": token, "token_fingerprint": token_key, "project_id": project_id, "proxy": proxy, "credits": None, "tier": "", "error": ""}
    if not token:
        result["error"] = "empty token"
        return result
    try:
        request_obj = urllib.request.Request(
            "https://aisandbox-pa.googleapis.com/v1/credits",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "*/*",
                "Origin": "https://labs.google",
                "Referer": "https://labs.google/",
                "User-Agent": "Mozilla/5.0",
            },
            method="GET",
        )
        normalized_proxy = _reference_credit_normalize_proxy(proxy)
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": normalized_proxy, "https": normalized_proxy})
        ) if normalized_proxy else urllib.request.build_opener()
        with opener.open(request_obj, timeout=timeout) as response:
            status = getattr(response, "status", None) or response.getcode()
            body = response.read().decode("utf-8", errors="replace")
        if int(status) != 200:
            result["error"] = f"HTTP {status}: {body[:300]}"
            return result
        data = json.loads(body or "{}")
        result.update({
            "ok": True,
            "credits": data.get("credits", 0),
            "tier": data.get("userPaygateTier") or data.get("serviceTier") or "UNKNOWN",
            "raw": data,
        })
        return result
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        result["error"] = f"HTTP {getattr(exc, 'code', '')}: {body[:300] or exc}"
        return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result


POLL_INITIAL_DELAY = int(os.getenv("VEO_POLL_INITIAL_DELAY", "7"))  # giống tool_veo3_mau: poll sớm để không trễ completion
POLL_INTERVAL = int(os.getenv("VEO_POLL_INTERVAL", "7"))  # giây giữa mỗi lần poll
POLL_MAX_WAIT = int(os.getenv("VEO_POLL_MAX_WAIT", "600"))  # timeout tối đa (giây) — ngân sách tổng cho 1 lần poll
POLL_SILENT_TIMEOUT = int(os.getenv("VEO_POLL_SILENT_TIMEOUT", "180"))  # giây — hủy task nếu không nhận được phản hồi hữu ích liên tục
ACTIVITY_MAX_AGE = int(os.getenv("VEO_ACTIVITY_MAX_AGE", "300"))  # activity > giá trị này coi là stuck, tự xóa
DEFERRED_UPSCALE_POLL_INTERVAL = int(os.getenv("VEO_UPSCALE_POLL_INTERVAL", "7"))  # check 1080p nhanh, không block worker
DEFERRED_UPSCALE_MAX_WAIT = int(os.getenv("VEO_UPSCALE_MAX_WAIT", "180"))  # timeout chờ 1080p, sau đó giữ 720p


# ───────────────────────────────────────────────────────────
# Helpers (copy từ veo_worker.py)
# ───────────────────────────────────────────────────────────


def _extract_video_url(poll_resp: dict) -> Optional[str]:
    """Tìm video URL trong nested poll response từ Google Labs."""
    if not isinstance(poll_resp, dict):
        return None

    def _search(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, str) and v.startswith("http"):
                    kl = k.lower()
                    if any(
                        x in kl for x in ("url", "uri", "download", "video", "link")
                    ):
                        return v
                    if any(
                        x in v.lower()
                        for x in (
                            "storage.googleapis",
                            ".mp4",
                            "generativelanguage",
                            "video",
                        )
                    ):
                        return v
                r = _search(v)
                if r:
                    return r
        elif isinstance(obj, list):
            for v in obj:
                r = _search(v)
                if r:
                    return r
        return None

    for op in poll_resp.get("operations", []):
        gens = op.get("mediaGenerations", [])
        for g in gens:
            url = (
                g.get("videoUrl")
                or g.get("downloadUrl")
                or g.get("uri")
                or g.get("url")
            )
            if url:
                return url
        resp_field = op.get("response") or op.get("result")
        if resp_field:
            u = _search(resp_field)
            if u:
                return u
    return _search(poll_resp)


def _extract_media_id(poll_resp: dict) -> Optional[str]:
    """Tìm mediaId gốc trong nested poll response để phục vụ Upscale 1080p."""
    if not isinstance(poll_resp, dict):
        return None

    def _search(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if (
                    k in ("mediaId", "mediaGenerationId", "media_id")
                    and isinstance(v, str)
                    and len(v) > 5
                ):
                    return v
                r = _search(v)
                if r:
                    return r
        elif isinstance(obj, list):
            for v in obj:
                r = _search(v)
                if r:
                    return r
        return None

    for op in poll_resp.get("operations", []):
        gens = op.get("mediaGenerations", [])
        for g in gens:
            mid = g.get("mediaId")
            if mid:
                return mid
        resp_field = op.get("response") or op.get("result")
        if resp_field:
            u = _search(resp_field)
            if u:
                return u
    return _search(poll_resp)


def _extract_status(poll_resp: dict) -> str:
    """Trích xuất status string từ poll response."""
    if not isinstance(poll_resp, dict):
        return "UNKNOWN"
    for op in poll_resp.get("operations", []):
        st = op.get("status", "")
        if st:
            return st
        gens = op.get("mediaGenerations", [])
        for g in gens:
            st2 = g.get("status", "")
            if st2:
                return st2
        op_obj = op.get("operation", {})
        if isinstance(op_obj, dict):
            if op_obj.get("done"):
                return "MEDIA_GENERATION_STATUS_COMPLETE"
    return "UNKNOWN"


def _download_video(
    url: str, dest_path: str, headers: dict = None, aspect: str = "9:16"
):
    """
    Download video URL trực tiếp về file local, không gọi FFMPEG can thiệp.
    Có retry 3 lần nếu đứt kết nối.
    """
    import urllib.request
    import time

    logger.info(f"[Download] Bắt đầu tải video trực tiếp (không FFMPEG): {dest_path}")
    req = urllib.request.Request(url, headers=headers or {})
    
    last_err = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=120) as r, open(dest_path, "wb") as f:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
            
            # Kiem tra file size de dam bao khong tai file rong
            if os.path.exists(dest_path) and os.path.getsize(dest_path) > 1024:
                logger.info(f"[Download] Đã tải xong video gốc (lần {attempt}): {dest_path}")
                return True
            else:
                raise Exception("File tải về quá nhỏ hoặc rỗng.")
                
        except Exception as e:
            last_err = e
            logger.warning(f"[Download] Lỗi khi tải video (lần {attempt}/3): {e}")
            time.sleep(2 * attempt)
            
    # Neu chay het 3 lan van loi -> Raise len cho caller
    logger.error(f"[Download] Thất bại hoàn toàn sau 3 lần tải: {last_err}")
    raise Exception(f"Không thể tải file video sau 3 lần rớt mạng: {last_err}")


# ───────────────────────────────────────────────────────────
# VeoService
# ───────────────────────────────────────────────────────────


import concurrent.futures
import queue as _queue_module


class VeoService:
    """
    End-to-end service: nhận params → tạo video Google Labs → download → trả file path.
    Hỗ trợ smart queue: giới hạn số luồng đồng thời, ưu tiên dùng cookie rảnh.
    """

    def __init__(
        self, veo_client, output_dir: str = "outputs", max_concurrent: int = None
    ):
        self.client = veo_client
        self.output_dir = os.path.abspath(output_dir)
        os.makedirs(self.output_dir, exist_ok=True)

        if max_concurrent is None:
            try:
                max_concurrent = int(store.get_setting("max_concurrent", 15))
            except Exception:
                max_concurrent = 15
        self._max_concurrent = min(5, max(1, int(max_concurrent)))

        # Khởi tạo queue
        # ─── SINGLE QUEUE: VEO3 (Imagen) ───
        self._task_queue_veo: _queue_module.Queue = _queue_module.Queue()

        # Biến đếm real-time thay thế Semaphore
        self._active_veo_tasks = 0
        self._concurrency_cond = threading.Condition()

        self._semaphore_veo = threading.Semaphore(self._max_concurrent)
        self._semaphore = self._semaphore_veo

        # ─── Per-account concurrency counter ───
        self._cookie_busy_veo: dict = {}  # {account_name: int}
        self._cookie_cond = threading.Condition(threading.Lock())
        # Giới hạn cứng theo yêu cầu: 1 token/account = 1 Chrome, tối đa 3 lane song song.
        # Áp dụng cho cả legacy recovery/browser-task flow để không còn 10 task/account.
        self._PER_TOKEN_LANES = 3
        self._MAX_IMAGE_PER_ACCOUNT = self._PER_TOKEN_LANES
        self._MAX_VIDEO_PER_ACCOUNT = self._PER_TOKEN_LANES

        self._image_task_counter: int = 0
        # Reference implementation owns a Chrome-backed scheduler. Even when the
        # web queue has many workers, only one reference batch may enter it at a
        # time to avoid spawning multiple Chrome runtimes in parallel.
        self._reference_runtime_lock = threading.Lock()
        self._banana_lanes: dict = {}
        self._banana_lanes_lock = threading.Lock()

        # ── Banana: Lane Manager (per-account serialised create) ──
        self._lane_manager = LaneManager()

        # ─── Dedup set: tránh enqueue cùng task_id 2 lần ───
        self._queued_task_ids: set = set()
        self._queued_lock = threading.Lock()

        # ─── Track thời điểm task bắt đầu chạy thật (để timeout chính xác) ───
        self._task_started_at: dict = {}  # {task_id: timestamp}

        # ─── Track thời điểm task enqueue lần cuối (queue timeout dùng cái này
        # thay vì created_at, để khi user retry task cũ → reset đồng hồ chờ) ───
        self._task_enqueued_at: dict = {}  # {task_id: timestamp}

        # ─── Proxy health tracking: {account_name: 'alive'|'dead'} ───
        self._proxy_health: dict = {}

        # ─── Rate limiting tracker (timestamp only, không enforce cooldown cứng) ───
        self._last_image_request: dict = {}  # {account_name: timestamp}
        self._img_cooldown = 0  # Đã bỏ cooldown cứng — dùng 403-retry thay thế
        # ─── PER-ACCOUNT API GATE: mỗi account chờ riêng, đa account song song ───
        # Tự tạo entry cho mỗi account khi gọi API. N accounts = N gate song song.
        self._per_account_api_lock = threading.Lock()
        self._per_account_api_ts: dict = {}  # {account_name: last_api_timestamp}
        self._PER_ACCOUNT_MIN_INTERVAL = 5.0  # 5s giữa mỗi request CÙNG 1 account
        # ─── Daily quota backoff: cấm account khi hết daily quota (1 giờ) ───
        self._account_daily_quota_exhausted: dict = (
            {}
        )  # {account_name: exhausted_until_timestamp}
        self._DAILY_QUOTA_BACKOFF_SECONDS = 30  # 1 giờ khi daily quota hết
        # ─── Blacklist: account create_project FAILED → cấm đến khi cookie refresh ───
        self._account_project_blacklist: set = set()  # {account_name}

        # ─── Captcha Stats Tracker (thống kê captcha per account) ───
        # {account_name: {"remote_received": int, "remote_pass": int, "remote_fail": int,
        #                 "extension_received": int, "extension_pass": int, "extension_fail": int}}
        self._captcha_stats: dict = {}
        self._captcha_stats_lock = threading.Lock()

        # ─── Ban Reason Tracker (lý do tại sao account bị ban) ───
        self._ban_reasons: dict = {}  # {account_name: "lý do ban"}
        # ─── UNUSUAL_ACTIVITY: time-based retry window ───
        # Retry liên tục mỗi 5s trong vòng 2 phút → xoay proxy sau 2 phút
        self._unusual_activity_counts: dict = {}  # {account_name: int} — đếm số lần
        self._unusual_activity_start_ts: dict = {}  # {account_name: timestamp} — mốc bắt đầu
        self._last_unusual_rotated_proxy: dict = {}  # {account_name: str} — proxy mới sau 2 phút
        # ─── 403 retry: time-window 2 phút ───
        # Retry captcha liên tục trong 2 phút, xoay proxy sau mỗi 2 phút
        self._account_403_fails: dict = {}  # {account_name: int}
        self._account_403_start_ts: dict = {}  # {account_name: float} — mốc bắt đầu window 2 phút
        # ─── IP Error tracker: Google trả PUBLIC_ERROR_IP_INPUT_IMAGE khi proxy bị chặn ───
        self._account_ip_errors: dict = {}  # {account_name: int}


        # Runtime tạo ảnh/tạo video chạy direct; proxy UI/API/DB vẫn giữ để tương thích.
        self.disable_media_proxy = str(os.getenv("VEO_DISABLE_MEDIA_PROXY", "1")).lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        if self.disable_media_proxy:
            _filelog("[Proxy] Media runtime proxy DISABLED — create image/video runs DIRECT")

        # ─── KiotProxy API Integration (thay thế static proxy) ───
        # Cache kết quả từ KiotProxy API, auto-refresh khi hết TTL
        self._kiotproxy_cache: dict = {}   # {"http": "ip:port", "host": ..., "location": ..., "ttl": ..., "ttc": ..., "fetched_at": float}
        self._kiotproxy_lock = threading.Lock()
        self._kiotproxy_last_fetch: float = 0  # timestamp lần cuối gọi API

        # ─── KiotProxy POOL (N key / M account per key) ───
        # Pool cache: {key_str: {"http","ttl","ttc","fetched_at","location","host","realIpAddress","error"}}
        self._kiotproxy_pool_cache: dict = {}
        self._kiotproxy_pool_lock = threading.Lock()
        self._KIOTPROXY_POOL_REFRESH_TICK = 30  # giây giữa mỗi lần check TTC

        # ─── KiotProxy rotate local cooldown (chống spam /new API) ───
        # User nhớ: 2 phút có thể đổi IP. Nếu KP trả "chưa đến hạn đổi. Gửi lại sau N giây"
        # → tôn trọng N. Nếu gặp rate-limit → cooldown mặc định.
        self._KIOTPROXY_ROTATE_DEFAULT_COOLDOWN = 120   # 2 phút — KiotProxy tối đa 2 phút mới được đổi 1 lần
        self._KIOTPROXY_ROTATE_RATELIMIT_COOLDOWN = 120  # 2 phút — tôn trọng giới hạn KiotProxy
        self._kiotproxy_rotate_cooldown_until: dict = {}  # {key_str: ts_earliest_retry}
        self._kiotproxy_rotate_lock = threading.Lock()

        # ─── 401 upload blacklist (chống retry vô hạn khi account bị 401 liên tục) ───
        # Account upload 401 N lần liên tiếp → blacklist 5 phút → _claim_idle_cookie skip.
        self._UPLOAD_401_THRESHOLD = 3
        self._UPLOAD_401_BLACKLIST_SEC = 300  # 5 phút
        self._account_upload_401_fails: dict = {}            # {acc_name: consecutive_fail_count}
        self._account_upload_401_blacklist_until: dict = {}  # {acc_name: ts_until}

        # ─── Enqueue throttle (khi queue quá tải, enqueue chậm lại) ───
        self._ENQUEUE_THROTTLE_THRESHOLD = 50  # queue > 50 → delay lâu hơn
        self._ENQUEUE_THROTTLE_SLOW_SEC = 5    # 5s/task khi quá tải
        self._ENQUEUE_THROTTLE_NORMAL_SEC = 2  # 2s/task bình thường

        # ─── Cache Google Labs project ID per (account, UI project) ───
        # Mỗi UI project trên mỗi account có GL project riêng.
        # Tối đa 700 ảnh/GL project — khi đầy sẽ tự tạo project mới.
        self.MAX_IMAGES_PER_GL_PROJECT: int = 700
        self._gl_project_cache: dict = (
            {}
        )  # {(account_name, ui_project_id): {"id": gl_project_id, "count": int}}
        # Lock per (account, ui_project) → tránh race condition khi nhiều tasks cùng thấy cache rỗng
        self._project_locks: dict = (
            {}
        )  # {(account_name, ui_project_id): threading.Lock()}
        self._project_locks_meta = threading.Lock()  # bảo vệ việc tạo lock mới

        # ─── Real-time Activity Tracker (Giám sát hoạt động từng account) ───
        self._account_activity: dict = {}   # {account_name: {"phase": str, "task_id": str, "since": float}}
        self._activity_lock = threading.Lock()

        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self._max_concurrent * 2 + 4
        )

        # ─── Per-account task cooldown (giữa các task) ───
        # Đã bỏ cooldown để account lấp đầy ngay slot song song:
        # ảnh tối đa 10 task/account, video tối đa 15 task/account theo gate claim slot.
        self._per_account_task_ts: dict = {}  # {account_name: last_task_finished_ts}
        self._PER_ACCOUNT_TASK_INTERVAL = 0.0  # không nghỉ giữa 2 task ẢNH cùng account
        self._PER_ACCOUNT_VIDEO_INTERVAL = 0.0  # không nghỉ giữa 2 task VIDEO cùng account
        self._PER_ACCOUNT_UPSAMPLE_INTERVAL = 0.0  # không nghỉ thêm sau task ẢNH có upsample 4K/2K
        # Timestamp task có upsample gần nhất — so sánh với last_task_ts để override interval
        self._last_upsample_completed_ts: dict = {}  # {account_name: ts}
        self._cookie_wait_log_ts: dict = {}  # rate-limit log chờ slot/cooldown

        # ─── Khởi động Worker Pool ───
        self._workers_veo: list = []
        _n_veo = max(1, int(self._max_concurrent))
        for _ in range(_n_veo):
            t = threading.Thread(
                target=self._queue_worker,
                args=(self._task_queue_veo, self._semaphore_veo, "veo"),
                daemon=True,
            )
            t.start()
            self._workers_veo.append(t)

        self._workers = self._workers_veo

        _filelog(
            f"[Init] VeoService started. "
            f"veo_workers={_n_veo} (max_concurrent={self._max_concurrent})"
        )

        # ─── Project Tracker: log + persist started_at/finished_at khi project xong ───
        try:
            from core.project_tracker import start as _start_proj_tracker
            _start_proj_tracker()
        except Exception as _e:
            logger.warning(f"[Init] Project tracker start fail: {_e}")

        # ─── KiotProxy Pool: disabled for media runtime ───
        if self.disable_media_proxy:
            _filelog("[Init] KiotProxy pool refresh thread SKIPPED — media runtime runs DIRECT")
        else:
            try:
                _t_pool = threading.Thread(
                    target=self._kiotproxy_pool_refresh_loop, daemon=True
                )
                _t_pool.start()
                _filelog("[Init] KiotProxy pool refresh thread started")
            except Exception as _e:
                _filelog(f"[Init] KiotProxy pool thread FAILED: {_e}")

        # ─── Deferred Upscale 1080p Polling (background thread) ───
        self._deferred_upscale_queue: list = []  # [{task_id, op_name, scene_id, video_url_720, file_path, client, submitted_at, next_check_at, account_name}]
        self._deferred_upscale_lock = threading.Lock()
        try:
            _t_upscale = threading.Thread(
                target=self._deferred_upscale_loop, daemon=True
            )
            _t_upscale.start()
            _filelog("[Init] Deferred upscale 1080p polling thread started")
        except Exception as _e:
            _filelog(f"[Init] Deferred upscale thread FAILED: {_e}")

        # ─── Startup Recovery: re-enqueue tasks PENDING/PROCESSING còn sót từ DB ───
        def _startup_recovery():
            import time as _t

            _t.sleep(3)  # Chờ workers sẵn sàng
            try:
                from web.models import TaskStatus as _TS, ActionType as _AT

                # Xóa dedup cache từ session cũ. Trước đây code giữ lại toàn bộ
                # task PROCESSING trong DB như "currently running". Sau đó lại reset
                # PROCESSING -> PENDING, nên enqueue bị SKIP dup vì ID vẫn nằm trong
                # _queued_task_ids => task kẹt PROCESSING/PENDING giả, không worker nào chạy.
                with self._queued_lock:
                    _old_count = len(self._queued_task_ids)
                    self._queued_task_ids.clear()
                    _filelog(
                        f"[Recovery] Cleared {_old_count} stale dedup-IDs từ session trước "
                        "reason=restart_reset_processing_before_reenqueue"
                    )

                # ── CRITICAL: Reset per-account busy counter ──
                # Khi server restart, các task cũ đã chết nhưng counter 
                # _cookie_busy_veo vẫn giữ giá trị cũ → account "busy" vĩnh viễn
                # → KHÔNG task nào claim được account → hệ thống đứng im.
                _stale_busy = {k: v for k, v in self._cookie_busy_veo.items() if v > 0}
                if _stale_busy:
                    _filelog(
                        f"[Recovery] ⚠️ Reset {len(_stale_busy)} stale busy counters: "
                        f"{_stale_busy}"
                    )
                self._cookie_busy_veo.clear()
                self._active_veo_tasks = 0
                self._task_started_at.clear()
                _filelog(
                    "[Recovery] ✅ Cleared runtime state: "
                    "_cookie_busy_veo, _active_veo_tasks, _task_started_at "
                    "(restart-safe scheduler will rebuild queue/batches)"
                )

                # Bước 1: Reset tất cả PROCESSING → PENDING (zombie từ lần chạy trước)
                # Sau restart, runtime/thread/Chrome session cũ đã mất nên PROCESSING
                # trong DB chỉ còn là Run ảo. Reset về PENDING để enqueue lại và
                # dispatcher mới chia theo capacity token_count * 3.
                processing, _ = store.list_video_tasks(status="PROCESSING", limit=20000)
                reset_processing_count = 0
                for task in processing:
                    raw = task.raw_result if isinstance(task.raw_result, dict) else {}
                    public_dispatch = raw.get("public_dispatch") if isinstance(raw.get("public_dispatch"), dict) else {}
                    if public_dispatch.get("assigned_machine_id"):
                        # Public-dispatched jobs must stay PROCESSING so the assigned
                        # Banana worker can claim them through /api/machines/jobs/claim.
                        # Resetting them to PENDING re-enqueues them into the legacy
                        # local VeoService queue and causes direct local execution.
                        continue
                    task.status = _TS.PENDING
                    task.error = None
                    task.picked_account_name = None
                    store.update_video_task(task)
                    reset_processing_count += 1
                _filelog(
                    f"[Recovery] Reset PROCESSING -> PENDING count={reset_processing_count} "
                    "reason=server_restart_zombie_processing"
                )

                # Bước 2: Enqueue TẤT CẢ PENDING (giới hạn 20000 thay vì 5000)
                pending, total_pending = store.list_video_tasks(
                    status="PENDING", limit=20000
                )
                stuck = list(pending)
                if not stuck:
                    _filelog("[Recovery] Không có task PENDING nào cần recover.")
                    return
                _filelog(
                    f"[Recovery] Tìm thấy {len(stuck)}/{total_pending} task PENDING cần re-enqueue "
                    "vào queue để scheduler chia lại theo token_capacity."
                )
                _I2V_TYPES = {
                    _AT.IMAGE_TO_VIDEO,
                    _AT.IMAGES_TO_VIDEO,
                    _AT.FRAMES_TO_VIDEO,
                }
                _IMAGE_TYPES_SET = {_AT.CREATE_IMAGE}
                _timeout_count = 0
                _enqueue_count = 0
                for idx, task in enumerate(stuck):
                    # Recovery = user resume sau restart → reset đồng hồ chờ
                    # về now để task tính là vừa enqueue (không chết oan vì
                    # created_at từ nhiều ngày trước).
                    _tid = getattr(task, "id", None)
                    if _tid:
                        self._task_enqueued_at[_tid] = _t.time()
                    # ── Timeout check: fail task cũ TRƯỚC khi enqueue ──
                    if self._check_task_timeout(task):
                        _timeout_count += 1
                        _t.sleep(0.05)  # Yield cho HTTP server (tránh lock starvation của MongoDB)
                        continue

                    if idx > 0:
                        _t.sleep(0.5)
                    at = getattr(task, "action_type", None)
                    if at == _AT.CREATE_IMAGE:
                        run_fn = getattr(self, "_run_create_image", None)
                    elif at in _I2V_TYPES:
                        run_fn = getattr(self, "_run_i2v", None)
                    else:
                        run_fn = self._run_t2v
                    if run_fn:
                        self.enqueue(task, run_fn)
                        _enqueue_count += 1
                        _filelog(f"[Recovery] Re-enqueued task={task.id} type={at}")
                    else:
                        _filelog(
                            f"[Recovery] Bỏ qua task={task.id}: không tìm được run_fn for type={at}"
                        )
                if _timeout_count > 0:
                    _filelog(
                        f"[Recovery] ⏰ Auto-failed {_timeout_count} task(s) quá timeout, "
                        f"re-enqueued {_enqueue_count} task(s) còn valid."
                    )
            except Exception as e:
                _filelog(f"[Recovery] Lỗi startup recovery: {e}")

        threading.Thread(target=_startup_recovery, daemon=True).start()

        # ─── Background Reaper: Hủy tự động các task treo PENDING/PROCESSING quá timeout ───
        def _task_reaper_loop():
            import time as _t
            from web.models import TaskStatus as _TS
            while True:
                _t.sleep(60)  # Chạy 1 phút 1 lần
                try:
                    pending, _ = store.list_video_tasks(status="PENDING", limit=5000)
                    processing, _ = store.list_video_tasks(status="PROCESSING", limit=5000)
                    recovered_count = 0
                    for t in list(pending):
                        self._check_task_timeout(t)
                    for t in list(processing):
                        if self._recover_fake_processing_task(t):
                            recovered_count += 1
                            continue
                        self._check_task_timeout(t)
                    if recovered_count:
                        _filelog(f"[Reaper] Recovered fake PROCESSING tasks count={recovered_count}")
                except Exception as _e:
                    _filelog(f"[Reaper] Lỗi dọn rác task: {_e}")

        threading.Thread(target=_task_reaper_loop, daemon=True).start()

        # ─── Timeout constants ───
        self.VIDEO_TASK_TIMEOUT = 1200   # 20 phút cho video (T2V, I2V)
        self.IMAGE_TASK_TIMEOUT = 600    # 10 phút cho tạo ảnh

    def _run_fn_for_task(self, task):
        """Chọn worker phù hợp để re-enqueue task từ recovery/reaper."""
        try:
            from web.models import ActionType
            at = getattr(task, "action_type", None)
            if at == ActionType.CREATE_IMAGE:
                return getattr(self, "_run_create_image", None)
            if at in (ActionType.IMAGE_TO_VIDEO, ActionType.IMAGES_TO_VIDEO, ActionType.FRAMES_TO_VIDEO):
                return getattr(self, "_run_i2v", None)
        except Exception:
            pass
        return getattr(self, "_run_t2v", None)

    def _recover_fake_processing_task(self, task) -> bool:
        """Reset/re-enqueue PROCESSING giả chưa từng được worker chạy thật.

        Dấu hiệu fake: DB là PROCESSING nhưng không có started_at trong RAM,
        không có picked account, không có operation/url/file/error và updated_at
        đã đứng yên vài phút. Trường hợp này thường xảy ra khi restart recovery
        reset/enqueue bị dedup stale chặn.
        """
        tid = getattr(task, "id", None)
        if not tid or tid in self._task_started_at:
            return False
        raw = getattr(task, "raw_result", None) if isinstance(getattr(task, "raw_result", None), dict) else {}
        result = raw.get("result") if isinstance(raw.get("result"), dict) else {}
        has_runtime_signal = any(
            [
                getattr(task, "picked_account_name", None),
                raw.get("account_name"),
                result.get("account_name"),
                result.get("attempts"),
                result.get("op_name"),
                result.get("download_url"),
                result.get("saved_path"),
                getattr(task, "media_id", None),
                getattr(task, "output_filename", None),
                getattr(task, "error", None),
            ]
        )
        if has_runtime_signal:
            return False
        try:
            updated_at = float(getattr(task, "updated_at", None) or getattr(task, "created_at", None) or 0)
        except Exception:
            updated_at = 0
        if updated_at and time.time() - updated_at < 180:
            return False
        from web.models import TaskStatus
        with self._queued_lock:
            self._queued_task_ids.discard(tid)
        task.status = TaskStatus.PENDING
        task.error = None
        task.picked_account_name = None
        store.update_video_task(task)
        run_fn = self._run_fn_for_task(task)
        if run_fn:
            self.enqueue(task, run_fn)
            _filelog(f"[Reaper] Reset fake PROCESSING -> PENDING and re-enqueued task={tid}")
            return True
        _filelog(f"[Reaper] Fake PROCESSING task={tid} reset but no run_fn found")
        return False

    def _check_task_timeout(self, task) -> bool:
        """Kiểm tra task đã vượt timeout chưa. Nếu vượt → auto FAILED và return True.

        Logic 2 tầng:
        - Task ĐANG CHẠY (có started_at): timeout tính từ lúc bắt đầu chạy (1200s/1800s)
        - Task CHỜ TRONG QUEUE (chưa started): timeout dài hơn (gấp 3x) để tránh cancel
          task chưa kịp chạy khi queue đông.
        """
        created = getattr(task, "created_at", None)
        if not created:
            return False
        try:
            if hasattr(created, "timestamp"):
                created_ts = created.timestamp()
            else:
                created_ts = float(created)
        except Exception:
            return False

        from web.models import ActionType, TaskStatus
        at = getattr(task, "action_type", None)
        base_timeout = self.IMAGE_TASK_TIMEOUT if at == ActionType.CREATE_IMAGE else self.VIDEO_TASK_TIMEOUT

        tid = getattr(task, "id", None)
        started_at = self._task_started_at.get(tid) if tid else None

        now = time.time()
        if started_at:
            # Task đang chạy → tính từ lúc bắt đầu
            running_time = now - started_at
            if running_time <= base_timeout:
                return False
            age_display = int(running_time)
            timeout_display = int(base_timeout)
            timeout_label = f"running {age_display}s > {timeout_display}s"
        else:
            # Task chờ trong queue → cho thêm thời gian (gấp 3x).
            # Tính tuổi từ lần ENQUEUE gần nhất (không phải created_at gốc) để
            # khi user retry hoặc task bị re-enqueue (pinned busy) thì đồng hồ
            # chờ được reset, tránh reap oan task vừa retry.
            queue_timeout = base_timeout * 3
            enqueued_ts = self._task_enqueued_at.get(tid, created_ts) if tid else created_ts
            age_from_enqueue = now - enqueued_ts
            if age_from_enqueue <= queue_timeout:
                return False
            age_display = int(age_from_enqueue)
            timeout_display = int(queue_timeout)
            timeout_label = f"queued {age_display}s > {timeout_display}s"

        task.status = TaskStatus.FAILED
        task.error = (
            f"⏰ Timeout: task {timeout_label} → auto-cancelled"
        )
        store.update_video_task(task)
        # Giải phóng account
        acc = getattr(task, "picked_account_name", None)
        if acc:
            try:
                self._release_cookie(acc)
                self._clear_activity(acc)
            except Exception:
                pass
        if tid:
            with self._queued_lock:
                self._queued_task_ids.discard(tid)
            self._task_started_at.pop(tid, None)
            self._task_enqueued_at.pop(tid, None)
        _filelog(
            f"[Timeout] ⏰ Task {tid or '?'} auto-cancelled "
            f"({timeout_label})"
        )
        return True

    # ── F5 helpers (no-swap retry) ──────────────────────────────────────────
    # User yêu cầu: lỗi như UNUSUAL_ACTIVITY / "too much" / quota / 5xx →
    # behavior giống user F5 trên web → reload tab + retry CÙNG account.
    # Không bao giờ swap account.

    @staticmethod
    def _is_f5_recoverable_error(err_str=None, status_code=None) -> bool:
        """True nếu lỗi từ Google API có thể recover bằng F5 + retry cùng
        account. False cho lỗi phía client / network không liên quan tới
        page state (timeout proxy, parse fail, 401 auth, 400 bad request).

        F5-able:
          - 403 UNUSUAL_ACTIVITY
          - 429 / "too much" / "too many" / "quota" / "RESOURCE_EXHAUSTED"
          - 5xx server error / "DEADLINE_EXCEEDED" / "internal server error"
            / "service unavailable" / "backend error"
        """
        try:
            sc = int(status_code) if status_code is not None else None
        except Exception:
            sc = None
        if sc is not None:
            if 500 <= sc < 600:
                return True
            if sc == 429:
                return True
        s = (err_str or "").lower()
        if not s:
            return False
        f5_keys = (
            "unusual_activity",
            "too much",
            "too many",
            "quota",
            "resource_exhausted", "resource exhausted",
            "deadline_exceeded", "deadline exceeded",
            "internal_server_error", "internal server error",
            "service_unavailable", "service unavailable",
            "backend error", "backend_error",
        )
        return any(k in s for k in f5_keys)

    def _trigger_f5(self, account_label, reason: str = "", sleep_s: float = 5.0) -> bool:
        """F5 tab labs.google của account qua bridge + sleep `sleep_s`.

        Trả True nếu bridge online + reload OK (đã sleep). False nếu bridge
        offline / reload fail (vẫn sleep `sleep_s` để tránh hammer API liên tục).
        """
        if not account_label:
            time.sleep(sleep_s)
            return False
        try:
            from core.browser_flow_client import BrowserFlowClient
            bfc = BrowserFlowClient(account_email=account_label)
            if not bfc.is_connected():
                logger.info(
                    f"[F5] {account_label} bridge OFFLINE → skip reload, sleep {sleep_s}s ({reason})"
                )
                time.sleep(sleep_s)
                return False
            ok = bfc.reload_tab(timeout_s=30)
            if ok:
                _filelog(f"[F5] {account_label} reload OK ({reason}) → sleep {sleep_s}s")
                time.sleep(sleep_s)
                return True
            logger.warning(
                f"[F5] {account_label} reload FAIL ({reason}) → sleep {sleep_s}s anyway"
            )
            time.sleep(sleep_s)
            return False
        except Exception as e:
            logger.warning(f"[F5] {account_label} exception ({reason}): {e}")
            time.sleep(sleep_s)
            return False


    # ── Banana §06: Centralized error handling for ALL flows ──────────

    def _handle_api_error_banana(
        self,
        api_detail: str,
        account_label: str,
        flow: str,
        task_client=None,
        img_client=None,
        task=None,
        captcha_action: str = "IMAGE_GENERATION",
    ) -> dict:
        """Centralized error handler using Banana error_table.

        Replaces 200+ LOC inline if/elif chains in _run_create_image,
        _run_t2v, _run_i2v, _run_i2v_b64.

        Returns dict:
            action, should_retry, should_increment, should_fail_task,
            fail_error, new_captcha, new_task_client, new_img_client
        """
        rv = {
            "action": None,
            "should_retry": True,
            "should_increment": True,
            "should_fail_task": False,
            "fail_error": None,
            "new_captcha": None,
            "new_task_client": None,
            "new_img_client": None,
        }

        action, meta = classify_error(api_detail)
        rv["action"] = action
        reason = meta.get("reason", "unknown")
        sleep_s = meta.get("sleep", 1)

        _filelog(
            f"[{flow}][ErrorTable] action={action.value} reason={reason} "
            f"detail={api_detail[:120]}"
        )

        # ── FAIL_PERMANENT (400 UNSAFE) ──
        if action == ErrorAction.FAIL_PERMANENT:
            rv["should_retry"] = False
            rv["should_increment"] = False
            rv["should_fail_task"] = True
            import re as _re_u
            _m = _re_u.search(r'"reason"\s*:\s*"([^"]+)"', api_detail)
            _unsafe_reason = _m.group(1) if _m else ""
            rv["fail_error"] = (
                f"[Mã lỗi: 400 — {_unsafe_reason or 'UNSAFE_GENERATION'}] "
                f"Prompt bị chặn bởi Google safety filter"
            )
            return rv

        # ── RELOAD_COOKIE_RETRY (401 / auth expired) ──
        if action == ErrorAction.RELOAD_COOKIE_RETRY:
            try:
                from core.captcha_pool import get_pool as _gp
                _pe = getattr(task_client, "_last_pool_entry", None)
                if _pe:
                    _gp().report_result(_pe, passed=False)
                    self._record_captcha_stat(account_label, _pe.source, "fail")
            except Exception:
                pass
            try:
                _fresh_acc = store.get_veo_account_by_name(account_label)
                if _fresh_acc and _fresh_acc.cookie:
                    _px = self._resolve_proxy_for_account(_fresh_acc)
                    from core.hybrid_veo_client import HybridVeoClient as _HVC
                    _ntc = _HVC(_fresh_acc.cookie, proxy=_px, account_email=_fresh_acc.name)
                    if _ntc.access_token:
                        rv["new_task_client"] = _ntc
                        setattr(_ntc, "_account_label", account_label)
                        if img_client is not None:
                            from core.hybrid_imagen_client import HybridImagenClient as _HIC
                            rv["new_img_client"] = _HIC(
                                cookie=_fresh_acc.cookie, proxy=_px,
                                access_token=_ntc.access_token,
                                account_email=account_label,
                            )
                        logger.info(f"[{flow}] 401 → reload cookie OK")
            except Exception as _re:
                logger.warning(f"[{flow}] 401 reload cookie error: {_re}")
            _old_px = (getattr(img_client, "proxy", None) if img_client else None) or (
                getattr(task_client, "proxy", None) if task_client else None
            )
            try:
                from core.static_proxy_pool import get_fresh_proxy as _gfp, mark_proxy_dead as _mpd
                if _old_px: _mpd(_old_px)
                _nip = _gfp(exclude={_old_px} if _old_px else None)
                if _nip:
                    if img_client:
                        try: img_client.proxy = _nip
                        except: pass
                    if task_client:
                        try: task_client.proxy = _nip
                        except: pass
                    logger.info(f"[{flow}] 401 → proxy rotated: {_nip[:40]}")
            except Exception:
                pass
            rv["should_increment"] = False
            time.sleep(sleep_s)
            return rv

        # ── F5_RETRY (UNUSUAL_ACTIVITY, reCAPTCHA, TOO_MUCH, 5xx) ──
        if action == ErrorAction.F5_RETRY:
            if meta.get("rotate_proxy") == "static":
                try:
                    from core.static_proxy_pool import get_random_proxy
                    _npx = get_random_proxy()
                    if _npx:
                        if img_client:
                            try: img_client.proxy = _npx
                            except: pass
                        if task_client:
                            try: task_client.proxy = _npx
                            except: pass
                        _filelog(f"[{flow}] F5 → proxy tĩnh: {_npx[:50]}")
                except Exception:
                    pass
            self._trigger_f5(account_label, reason=reason, sleep_s=sleep_s)
            if meta.get("refresh_captcha") and task_client:
                if captcha_action == "VIDEO_GENERATION" and _veo_browser_runtime_enabled():
                    rv["new_captcha"] = "BROWSER_RUNTIME_RECAPTCHA_IN_PAGE"
                    _filelog(
                        f"[{flow}] F5 → browser runtime captcha placeholder "
                        "(skip legacy refresh)"
                    )
                else:
                    try:
                        setattr(task_client, "_account_label", account_label)
                        _fc = self._solve_captcha_smart(task_client, action=captcha_action)
                        if _fc:
                            rv["new_captcha"] = _fc
                    except Exception:
                        pass
            rv["should_increment"] = False
            return rv

        # ── ROTATE_PROXY_RETRY (proxy dead, IP filter) ──
        if action == ErrorAction.ROTATE_PROXY_RETRY:
            nip = None
            try: nip = self._rotate_kiotproxy_key_for_account(account_label)
            except Exception: pass
            if not nip:
                try:
                    from core.static_proxy_pool import get_random_proxy
                    nip = get_random_proxy()
                except Exception: pass
            if nip:
                if img_client:
                    try: img_client.proxy = nip
                    except: pass
                if task_client:
                    try: task_client.proxy = nip
                    except: pass
                logger.info(f"[{flow}] Proxy rotated → {nip[:40]}")
            time.sleep(sleep_s)
            rv["should_increment"] = False
            return rv

        # ── BACKOFF_RETRY (429, RESOURCE_EXHAUSTED, quota) ──
        if action == ErrorAction.BACKOFF_RETRY:
            try:
                self._trigger_f5(account_label, reason=reason, sleep_s=sleep_s)
            except Exception:
                time.sleep(sleep_s)
            rv["should_increment"] = False
            return rv

        # ── RETRY_SAME (generic/unknown) ──
        time.sleep(sleep_s)
        return rv

    # Queue Management

    def enqueue(self, task, run_fn):
        """Queue task vào hàng đợi VEO."""
        task_id = getattr(task, "id", None)
        with self._queued_lock:
            if task_id and task_id in self._queued_task_ids:
                _filelog(f"[Queue] SKIP dup enqueue task={task_id}")
                return
            if task_id:
                self._queued_task_ids.add(task_id)

        # Reset đồng hồ chờ trong queue mỗi lần enqueue (kể cả re-enqueue
        # do pinned-busy hoặc user retry) → reaper không giết oan.
        if task_id:
            self._task_enqueued_at[task_id] = time.time()

        self._task_queue_veo.put((task, run_fn))
        _filelog(
            f"[Queue-VEO] Enqueued task={task_id}"
            f" | veo_queue={self._task_queue_veo.qsize()}"
        )
        # Batch tracker — auto detect lượt chạy theo gap enqueue
        try:
            from core.batch_tracker import note_enqueue as _bt_enq
            _bt_enq()
        except Exception:
            pass

    def get_enqueue_delay(self) -> float:
        """Trả về số giây nên sleep giữa 2 lần enqueue task.
        Khi queue quá tải (> threshold) → delay lâu hơn để worker kịp thở."""
        try:
            qsize = self._task_queue_veo.qsize()
        except Exception:
            qsize = 0
        if qsize > getattr(self, "_ENQUEUE_THROTTLE_THRESHOLD", 50):
            return float(getattr(self, "_ENQUEUE_THROTTLE_SLOW_SEC", 5))
        return float(getattr(self, "_ENQUEUE_THROTTLE_NORMAL_SEC", 2))

    def get_queue_status(self) -> dict:
        """Return current queue and cookie status."""
        with self._cookie_cond:
            mem_busy = set(
                k for k, v in self._cookie_busy_veo.items() if v and v > 0
            )
        try:
            processing_tasks, _ = store.list_video_tasks(status="PROCESSING", limit=200)
            for t in processing_tasks:
                acct = getattr(t, "picked_account_name", None)
                if acct:
                    mem_busy.add(acct)
        except Exception:
            pass
        try:
            all_accounts = store.list_veo_accounts()
            all_active_names = set(a.name for a in all_accounts if a.is_active)
        except Exception:
            all_active_names = set()
        busy_cookies = sorted(mem_busy)
        free_cookies = sorted(all_active_names - mem_busy)
        return {
            "max_concurrent": getattr(self, "_max_concurrent", 0),
            "queue_pending": self._task_queue_veo.qsize(),
            "busy_cookies": busy_cookies,
            "free_cookies": free_cookies,
            "blacklisted_cookies": sorted(getattr(self, '_account_project_blacklist', set())),
            "active_workers": len(self._workers),
            "active_veo_tasks": getattr(self, "_active_veo_tasks", 0),
            "per_account_busy_veo": {k: v for k, v in self._cookie_busy_veo.items() if v > 0},
            "account_403_fails": dict(self._account_403_fails) if hasattr(self, '_account_403_fails') else {},
        }

    def update_max_concurrent(self, n: int, **kwargs):
        """Adjust max concurrent workers cho VEO3."""
        n = max(1, int(n))
        old = self._max_concurrent
        self._max_concurrent = n
        store.set_setting("max_concurrent", n)

        with getattr(self, "_concurrency_cond", threading.Condition()):
            self._concurrency_cond.notify_all()

        if hasattr(self, "_workers_veo") and len(self._workers_veo) < n:
            for _ in range(n - len(self._workers_veo)):
                t = threading.Thread(
                    target=self._queue_worker,
                    args=(self._task_queue_veo, None, "veo"),
                    daemon=True,
                )
                t.start()
                self._workers_veo.append(t)
                self._workers.append(t)

        _filelog(f"[Queue] max_concurrent_veo {old} -> {n}")
        logger.info(f"[Queue] max_concurrent veo={n}")

    # ─── Batch Pre-Upload Images ───

    def _pre_upload_images(self, tasks, ui_project_id: str):
        """Legacy wrapper — delegate sang _distribute_and_preupload.

        Caller (5 route batch Excel/retry/resume) tự enqueue sau khi hàm này
        return, vì vậy enqueue_after=False. Phương thức mới sẽ:
          - Chia n task cho x account (least-loaded)
          - Mỗi account upload bucket riêng → pin task vào account đó
          - Gán shared_gl_project + cached_mid + pinned_account_name
          - KHÔNG enqueue (caller enqueue)
        """
        return self._distribute_and_preupload(
            tasks, ui_project_id, enqueue_after=False
        )


    # ─── Multi-account bucket pre-upload ───

    def _distribute_and_preupload(
        self,
        tasks: list,
        ui_project_id: str,
        enqueue_after: bool = True,
    ) -> None:
        """
        Chia n task cho x account theo least-loaded, mỗi account upload riêng
        bucket của mình rồi pin task → account. Worker sau này BẮT BUỘC chạy
        task trên chính account đã upload (mediaId account-bound).

        Args:
            tasks: list VideoTask (đã create trong DB, chưa enqueue).
            ui_project_id: project id UI (làm fallback khi GL project không dựng được).
            enqueue_after: True → tự enqueue sau khi bucket xong; False → caller tự enqueue.

        Công thức chia:  x = min(ceil(n / 3), active_accounts, 10)

        Bucket lỗi (client init fail / không active account) → fallback:
          - enqueue_after=True: enqueue task KHÔNG pin → worker upload như flow cũ.
          - enqueue_after=False: task giữ nguyên, caller xử lý.

        Phương thức này chạy đồng bộ — caller phải submit vào executor nếu muốn
        non-blocking (ví dụ HTTP handler).
        """
        import concurrent.futures as _futures
        import math as _math
        from core.veo_client import VeoClient

        if not tasks:
            return

        # ── Step 1: Lọc task có ảnh path (upload được) ──
        def _task_paths(t):
            """Trả set các path tồn tại của task."""
            raw = t.raw_result or {}
            paths = set()
            for img in raw.get("images_b64") or []:
                if isinstance(img, dict):
                    p = img.get("path")
                    if p and os.path.exists(p):
                        paths.add(p)
            for p in (raw.get("image_path"), raw.get("end_image_path")):
                if p and os.path.exists(p):
                    paths.add(p)
            return paths

        eligible = [t for t in tasks if _task_paths(t)]
        non_eligible = [t for t in tasks if t not in eligible]

        if not eligible:
            logger.info("[PreUploadDist] Không có task nào có ảnh path → skip")
            if enqueue_after:
                for t in tasks:
                    self.enqueue(t, self._run_i2v_b64)
            return

        n = len(eligible)

        # ── Step 2: Lọc active accounts ──
        accounts = store.list_veo_accounts()
        now = time.time()
        active = []
        for a in accounts:
            if not getattr(a, "is_active", False):
                continue
            if a.name in getattr(self, "_account_project_blacklist", set()):
                continue
            _bl_until = self._account_upload_401_blacklist_until.get(a.name, 0)
            if _bl_until > now:
                continue
            active.append(a)

        if not active:
            logger.warning(
                "[PreUploadDist] Không có active account → fallback enqueue không pin"
            )
            if enqueue_after:
                for t in tasks:
                    self.enqueue(t, self._run_i2v_b64)
            return

        # ── Step 3: x = min(ceil(n/3), len(active), 10) ──
        x = min(max(1, _math.ceil(n / 3)), len(active), 10)

        # Least-loaded + LRU tie-break (giống _claim_idle_cookie)
        busy_dict = self._cookie_busy_veo
        active_sorted = sorted(
            active,
            key=lambda a: (
                busy_dict.get(a.name, 0),
                float(self._per_account_task_ts.get(a.name, 0) or 0),
            ),
        )
        picked = active_sorted[:x]

        logger.info(
            f"[PreUploadDist] n={n} tasks → x={x} uploader accounts: "
            f"{[a.name for a in picked]}"
        )

        # ── Step 4: Chia round-robin ──
        buckets: dict = {a.name: [] for a in picked}
        for i, t in enumerate(eligible):
            acc_name = picked[i % x].name
            buckets[acc_name].append(t)

        # Non-eligible (ví dụ task dùng base64 inline không có path) → fallback
        if non_eligible and enqueue_after:
            logger.info(
                f"[PreUploadDist] {len(non_eligible)} task không có path → enqueue không pin"
            )
            for t in non_eligible:
                self.enqueue(t, self._run_i2v_b64)

        acc_by_name = {a.name: a for a in picked}

        # ── Step 5: Process từng bucket ──
        def _process_bucket(acc_name: str, bucket_tasks: list):
            acc = acc_by_name[acc_name]

            # Build client (account này dùng luôn cho upload + worker sau này)
            try:
                resolved_proxy = self._resolve_proxy_for_account(acc)
                from core.hybrid_veo_client import HybridVeoClient as _HVC
                upload_client = _HVC(cookie=acc.cookie, proxy=resolved_proxy, account_email=acc.name)
                if not upload_client.access_token:
                    upload_client.get_session_token()
                if not upload_client.access_token:
                    raise RuntimeError("access_token empty after refresh")
            except Exception as e:
                logger.warning(
                    f"[PreUploadDist] Bucket {acc_name}: client init failed ({e}) "
                    f"→ {len(bucket_tasks)} task fallback không pin"
                )
                if enqueue_after:
                    for t in bucket_tasks:
                        self.enqueue(t, self._run_i2v_b64)
                return

            upload_client._account_label = acc_name

            # Tạo/reuse GL project trên account này
            gl_project = ui_project_id
            try:
                from core.project import create_project, search_user_projects

                existing = search_user_projects(
                    cookie=upload_client.cookie,
                    access_token=upload_client.access_token,
                    page_size=1,
                    timeout=8,
                    proxy=upload_client.proxy,
                )
                if existing and existing[0].get("projectId"):
                    gl_project = existing[0]["projectId"]
                    logger.info(
                        f"[PreUploadDist] Bucket {acc_name}: reuse GL project {gl_project[:20]}..."
                    )
                else:
                    new_proj = create_project(
                        f"API-{ui_project_id[:8]}",
                        tool_name="PINHOLE",
                        cookie=upload_client.cookie,
                        access_token=upload_client.access_token,
                        browser_headers=getattr(upload_client, "base_headers", None),
                        proxy=upload_client.proxy,
                    )
                    if new_proj:
                        gl_project = new_proj
                        logger.info(
                            f"[PreUploadDist] Bucket {acc_name}: created GL project {gl_project[:20]}..."
                        )
            except Exception as e:
                logger.warning(
                    f"[PreUploadDist] Bucket {acc_name}: GL project lookup failed ({e}), "
                    f"fallback UI project_id"
                )

            upload_client._current_project_id = gl_project

            # Dedupe paths trong bucket
            all_paths = set()
            for t in bucket_tasks:
                all_paths.update(_task_paths(t))

            # Aspect — từ task đầu
            first = bucket_tasks[0]
            aspect_str = (
                "IMAGE_ASPECT_RATIO_PORTRAIT"
                if getattr(first, "screen_ratio", "16:9") == "9:16"
                else "IMAGE_ASPECT_RATIO_LANDSCAPE"
            )

            path_to_mid: dict = {}
            if all_paths:
                _t0 = time.time()
                n_up = min(5, len(all_paths))

                def _up(p):
                    try:
                        mid, _err = self._upload_with_proxy_retry(
                            upload_client,
                            upload_client.upload_image_from_path,
                            p,
                            aspect=aspect_str,
                            project_id=gl_project,
                            flow="PreUpload",
                            task_id=acc_name,
                        )
                        return p, mid
                    except Exception as e:
                        logger.warning(
                            f"[PreUploadDist] Upload {os.path.basename(p)} on {acc_name}: {e}"
                        )
                        return p, None

                with _futures.ThreadPoolExecutor(
                    max_workers=n_up, thread_name_prefix=f"Up-{acc_name[:6]}"
                ) as ex:
                    for p, mid in ex.map(_up, list(all_paths)):
                        if mid:
                            path_to_mid[p] = mid

                elapsed = time.time() - _t0
                logger.info(
                    f"[PreUploadDist] Bucket {acc_name}: uploaded "
                    f"{len(path_to_mid)}/{len(all_paths)} ảnh trong {elapsed:.1f}s"
                )

            # Gán cached_mid + pin account
            for t in bucket_tasks:
                raw = t.raw_result or {}
                for img in raw.get("images_b64") or []:
                    if isinstance(img, dict):
                        p = img.get("path")
                        if p and p in path_to_mid:
                            img["cached_mid"] = path_to_mid[p]
                raw["shared_gl_project"] = gl_project
                t.raw_result = raw
                t.pinned_account_name = acc_name
                try:
                    store.update_video_task(t)
                except Exception as e:
                    logger.warning(f"[PreUploadDist] Update task {t.id} failed: {e}")

            # Enqueue (pinned worker sẽ honor)
            if enqueue_after:
                for t in bucket_tasks:
                    self.enqueue(t, self._run_i2v_b64)

        # ── Step 6: Chạy x bucket song song ──
        with _futures.ThreadPoolExecutor(
            max_workers=x, thread_name_prefix="PreUploadBucket"
        ) as bucket_ex:
            futs = [
                bucket_ex.submit(_process_bucket, acc_name, bucket_tasks)
                for acc_name, bucket_tasks in buckets.items()
                if bucket_tasks
            ]
            for f in _futures.as_completed(futs):
                try:
                    f.result()
                except Exception as e:
                    logger.error(f"[PreUploadDist] Bucket thread crash: {e}")

        _filelog(
            f"[PreUploadDist] Done — n={n} tasks, x={x} buckets, "
            f"project={ui_project_id[:12]}"
        )


    def _queue_worker(self, task_queue, semaphore, task_type: str):
        """Worker loop chuyên biệt cho task queue VEO."""
        while True:
            try:
                try:
                    task, run_fn = task_queue.get(timeout=1)
                except _queue_module.Empty:
                    continue

                # --- CONCURRENCY GATE ---
                cond = getattr(self, "_concurrency_cond", threading.Condition())
                with cond:
                    while getattr(self, "_active_veo_tasks", 0) >= getattr(
                        self, "_max_concurrent", 1
                    ):
                        cond.wait()
                    self._active_veo_tasks = (
                        getattr(self, "_active_veo_tasks", 0) + 1
                    )

                account_name = None
                _requeue_pinned_busy = False
                _initial_claimed_account = None
                try:
                    # ── CHECK: task có bị PAUSED trong lúc nằm trong queue không? ──
                    # Khi user nhấn "Tạm dừng", DB cập nhật PENDING→PAUSED
                    # nhưng task đã nằm trong RAM queue rồi → cần check lại
                    _fresh = store.get_video_task(task.id) if hasattr(task, 'id') else None
                    if _fresh and str(getattr(_fresh, 'status', '')).upper() in ('PAUSED', 'CANCELLED'):
                        logger.info(
                            f"[Queue-{task_type.upper()}] ⏸️ Task {task.id} đã bị PAUSED/CANCELLED "
                            f"trong lúc chờ → bỏ qua"
                        )
                        continue

                    # ── CHECK: có nguồn captcha nào sẵn sàng không? ──
                    # Nếu không có extension nào kết nối VÀ không có remote captcha
                    # → đặt task lại queue và chờ, tránh chạy task rồi fail vì thiếu captcha
                    _action_value = getattr(getattr(task, "action_type", None), "value", getattr(task, "action_type", None))
                    # Allow CREATE_IMAGE tasks to bypass manual account resolution and its cooldown
                    _is_banana_task = _action_value in ("CREATE_IMAGE", "CREATE_VIDEO_I2V", "CREATE_VIDEO_START_END_IMAGE")
                    if task_type == "veo" and not _is_banana_task:
                        _has_captcha_source = False
                        try:
                            from core.captcha_server import get_connected_accounts
                            if get_connected_accounts():
                                _has_captcha_source = True
                        except Exception:
                            pass
                        if not _has_captcha_source:
                            try:
                                from core.remote_captcha import is_server_available
                                if is_server_available():
                                    _has_captcha_source = True
                            except Exception:
                                pass
                        if not _has_captcha_source:
                            # Không có nguồn captcha → đặt lại queue, chờ 5s
                            task_queue.put((task, run_fn))
                            logger.warning(
                                f"[Queue-{task_type.upper()}] ⏳ Chưa có captcha source (extension/remote) "
                                f"→ giữ task {getattr(task, 'id', '?')} trong queue, chờ 5s..."
                            )
                            time.sleep(5)
                            continue

                    account_name = None
                    if not _is_banana_task:
                        account_name = self._claim_idle_cookie(task, task_type=task_type)
                    _initial_claimed_account = account_name  # Track account ban đầu worker claim

                    # ── Pinned account đầy slot → trả task lại cuối queue, pull task khác ──
                    # Tránh head-of-line blocking khi nhiều task pin cùng 1 account.
                    if account_name is None and getattr(task, '_pinned_busy_requeue', False):
                        try:
                            del task._pinned_busy_requeue
                        except Exception:
                            pass
                        _requeue_pinned_busy = True
                        # Sleep ngắn để tránh busy-loop nếu queue chỉ toàn task
                        # pin vào 1 account đầy slot.
                        time.sleep(0.3)
                        continue

                    # Ghi nhận thời điểm task bắt đầu chạy thật
                    self._task_started_at[task.id] = time.time()
                    _filelog(
                        f"[Queue-{task_type.upper()}] Running task={task.id}"
                        f' | cookie={account_name or "task-own"}'
                        f" | veo_q={self._task_queue_veo.qsize()}"
                    )
                    if account_name:
                        task.picked_account_name = account_name
                    run_fn(task)
                except Exception as e:
                    logger.error(
                        f'[Queue-{task_type.upper()}] Worker error task {getattr(task, "id", "?")}: {e}'
                    )
                finally:
                    # Cleanup started_at tracking
                    self._task_started_at.pop(getattr(task, 'id', None), None)
                    final_acc = (
                        getattr(task, "picked_account_name", None) or account_name
                    )
                    if final_acc:
                        self._release_cookie(final_acc, task_type=task_type)
                        self._clear_activity(final_acc)

                    # ── FIX: Release account ban đầu nếu task đã swap sang account khác ──
                    # Tránh counter leak: _claim_idle_cookie tăng busy cho account A,
                    # nhưng task bên trong swap sang account B → chỉ release B → A kẹt mãi mãi
                    _initial = getattr(task, '_initial_claimed_account', None) or _initial_claimed_account
                    if _initial and _initial != final_acc:
                        self._release_cookie(_initial, task_type=task_type)
                        self._clear_activity(_initial)
                        _filelog(
                            f"[Queue-{task_type.upper()}] 🔧 Released leaked counter for {_initial} "
                            f"(task swapped to {final_acc})"
                        )
                    task_id = getattr(task, "id", None)
                    if task_id:
                        with self._queued_lock:
                            self._queued_task_ids.discard(task_id)
                        # Cleanup enqueued_at (sẽ được set lại nếu re-enqueue ngay sau đây).
                        if not _requeue_pinned_busy:
                            self._task_enqueued_at.pop(task_id, None)

                    # ── Re-enqueue task khi pinned account đầy slot ──
                    # Đặt cuối queue để worker pull task khác trước.
                    if _requeue_pinned_busy:
                        try:
                            self.enqueue(task, run_fn)
                        except Exception as _e:
                            logger.error(
                                f"[Queue-{task_type.upper()}] Re-enqueue pinned-busy task {task_id} fail: {_e}"
                            )

                    # Trả lại slot
                    with cond:
                        self._active_veo_tasks = max(
                            0, getattr(self, "_active_veo_tasks", 1) - 1
                        )
                        cond.notify_all()

                    task_queue.task_done()
            except Exception as e:
                logger.error(f"[Queue-{task_type.upper()}] Worker loop crash: {e}")

    def _claim_idle_cookie(self, task, task_type: str = "veo") -> "str | None":
        """
        Tìm account còn slot và tăng counter.
        Giới hạn: mỗi account tối đa _MAX_IMAGE_PER_ACCOUNT task ẢNH đồng thời.

        ── Pinned account ──
        Nếu task.pinned_account_name set (do _distribute_and_preupload pin), CHỈ
        claim account đó — chờ vô thời hạn khi busy, KHÔNG bao giờ swap sang
        account khác. Account chết / blacklist → return None (caller FAIL task).
        """
        if getattr(task, "veo_cookie", None):
            return None  # task tự mang cookie riêng

        busy_dict = self._cookie_busy_veo
        pinned = getattr(task, "pinned_account_name", None)

        # Xác định loại task → áp dụng giới hạn per-account tương ứng
        from web.models import ActionType
        _action = getattr(task, "action_type", None)
        _is_image_task = _action == ActionType.CREATE_IMAGE
        _VIDEO_TYPES = {ActionType.TEXT_TO_VIDEO, ActionType.IMAGE_TO_VIDEO, ActionType.IMAGES_TO_VIDEO, ActionType.FRAMES_TO_VIDEO}
        _is_video_task = _action in _VIDEO_TYPES

        with self._cookie_cond:
            _claim_start = time.time()
            _CLAIM_TIMEOUT = 300  # 5 phút — safety net tránh worker kẹt vĩnh viễn
            while True:
                # ── Safety timeout: tránh deadlock khi counter bị leak ──
                if time.time() - _claim_start > _CLAIM_TIMEOUT:
                    _filelog(
                        f"[Queue] ⚠️ _claim_idle_cookie TIMEOUT sau {_CLAIM_TIMEOUT}s "
                        f"cho task {getattr(task, 'id', '?')} — trả None, worker sẽ re-queue"
                    )
                    try:
                        task._pinned_busy_requeue = True
                    except Exception:
                        pass
                    return None
                accounts = store.list_veo_accounts()
                try:
                    _deleted_account_names = set(store.list_deleted_accounts())
                except Exception:
                    _deleted_account_names = set()
                if _deleted_account_names:
                    _before_accounts = len(accounts)
                    accounts = [a for a in accounts if getattr(a, "name", "") not in _deleted_account_names]
                    if len(accounts) < _before_accounts:
                        _filelog(
                            f"[Queue] Deleted-account blacklist filter: {len(accounts)}/{_before_accounts} "
                            f"account usable → skip {sorted(_deleted_account_names)}"
                        )
                # Tự động cleanup blacklist upload_401 hết hạn
                _now_cd = time.time()
                _expired_bl = [
                    n for n, until in list(self._account_upload_401_blacklist_until.items())
                    if until <= _now_cd
                ]
                for n in _expired_bl:
                    self._account_upload_401_blacklist_until.pop(n, None)
                    _filelog(f"[Queue] Account {n} hết upload-401 blacklist, lại được dùng")

                # ── Pinned path: chỉ chờ account đã pin, không fallback ──
                if pinned:
                    if pinned in _deleted_account_names:
                        _filelog(
                            f"[Queue] Task {task.id}: PINNED account {pinned} nằm trong deleted-account blacklist "
                            f"→ clear pin và fallback account active khác"
                        )
                        try:
                            task.pinned_account_name = None
                            task.picked_account_name = None
                            store.update_video_task(task)
                        except Exception:
                            pass
                        pinned = None
                        continue
                    pinned_acc = next((a for a in accounts if a.name == pinned), None)
                    if not pinned_acc or not getattr(pinned_acc, "is_active", False):
                        _filelog(
                            f"[Queue] Task {task.id}: PINNED account {pinned} "
                            f"không tồn tại / inactive → clear pin và fallback account active khác"
                        )
                        try:
                            task.pinned_account_name = None
                            task.picked_account_name = None
                            store.update_video_task(task)
                        except Exception:
                            pass
                        pinned = None
                    elif pinned in self._account_project_blacklist:
                        _filelog(
                            f"[Queue] Task {task.id}: PINNED {pinned} bị project-blacklist "
                            f"→ clear pin và fallback account active khác"
                        )
                        try:
                            task.pinned_account_name = None
                            task.picked_account_name = None
                            store.update_video_task(task)
                        except Exception:
                            pass
                        pinned = None
                    if not pinned:
                        continue
                    # Upload-401 blacklist tạm thời: chờ hết hạn thay vì swap
                    _bl_until = self._account_upload_401_blacklist_until.get(pinned, 0)
                    if _bl_until > _now_cd:
                        now = time.time()
                        last_log = float(self._cookie_wait_log_ts.get(pinned, 0) or 0)
                        if now - last_log >= 10:
                            self._cookie_wait_log_ts[pinned] = now
                            _filelog(
                                f"[Queue] PINNED {pinned} đang upload-401 blacklist "
                                f"({int(_bl_until - _now_cd)}s còn lại) — task {task.id} chờ"
                            )
                        self._cookie_cond.wait(timeout=min(5, max(1, _bl_until - _now_cd)))
                        continue

                    if pinned_acc.name not in busy_dict:
                        busy_dict[pinned_acc.name] = 0
                    current = busy_dict.get(pinned_acc.name, 0)

                    # Cooldown video giữa các task cùng account (giữ invariant cũ)
                    if _is_video_task:
                        last_done = float(self._per_account_task_ts.get(pinned_acc.name, 0) or 0)
                        interval = float(getattr(self, "_PER_ACCOUNT_VIDEO_INTERVAL", 15) or 15)
                        remain = (last_done + interval) - time.time()
                        if remain > 0:
                            self._cookie_cond.wait(timeout=min(2, max(0.2, remain)))
                            continue

                    # Giới hạn slot: pinned busy → return None để worker re-queue task,
                    # tránh head-of-line blocking (worker đứng chờ → các task pin
                    # vào account khác cũng kẹt theo dù account đó rảnh).
                    if _is_video_task and current >= self._MAX_VIDEO_PER_ACCOUNT:
                        now = time.time()
                        last_log = float(self._cookie_wait_log_ts.get(pinned_acc.name, 0) or 0)
                        if now - last_log >= 10:
                            self._cookie_wait_log_ts[pinned_acc.name] = now
                            _filelog(
                                f"[Queue] PINNED {pinned_acc.name} bận "
                                f"({current}/{self._MAX_VIDEO_PER_ACCOUNT}) — task {task.id} re-queue, worker pull task khác"
                            )
                        try:
                            task._pinned_busy_requeue = True
                        except Exception:
                            pass
                        return None
                    if _is_image_task and current >= self._MAX_IMAGE_PER_ACCOUNT:
                        try:
                            task._pinned_busy_requeue = True
                        except Exception:
                            pass
                        return None

                    # Claim pinned account
                    busy_dict[pinned_acc.name] = current + 1
                    task.picked_account_name = pinned_acc.name
                    _filelog(
                        f"[Queue-{task_type.upper()}] claim account={pinned_acc.name} "
                        f"task={getattr(task, 'id', '?')} lanes={busy_dict[pinned_acc.name]}/{self._PER_TOKEN_LANES} "
                        "single_chrome_per_token=1 flow=legacy_recovery"
                    )
                    try:
                        store.update_video_task(task)
                    except Exception:
                        pass
                    return pinned_acc.name

                # ── Non-pinned path (flow cũ, giữ nguyên) ──
                active = []
                for a in accounts:
                    if not getattr(a, "is_active", False):
                        continue
                    # Skip account đang trong upload-401 blacklist
                    _bl_until = self._account_upload_401_blacklist_until.get(a.name, 0)
                    if _bl_until > _now_cd:
                        continue
                    active.append(a)

                # ── ƯU TIÊN account có Chrome bridge connected ──
                # Account chưa register bridge → đi httpx → bị UNUSUAL_ACTIVITY.
                # Nếu CÓ ít nhất 1 account có bridge → CHỈ pick từ subset đó.
                # Không có bridge nào → fallback toàn bộ active (httpx mode).
                try:
                    from core.browser_task_server import is_account_connected as _is_conn
                    _bridge_active = [a for a in active if _is_conn(a.name)]
                    if _bridge_active:
                        if len(_bridge_active) < len(active):
                            _filelog(
                                f"[Queue] Bridge filter: {len(_bridge_active)}/{len(active)} "
                                f"account có Chrome connected → skip {len(active)-len(_bridge_active)} non-bridge"
                            )
                        active = _bridge_active
                except Exception:
                    pass

                # ── Project-ready filter ──
                # Reference flow cần project_id đi cùng Gmail. Chỉ dùng account đã có
                # api_session.project_id để tránh xoay qua nhiều Gmail thiếu project.
                _project_ready = []
                for a in active:
                    _sess = getattr(a, "api_session", None)
                    _pid = ""
                    if isinstance(_sess, dict):
                        _pid = str(_sess.get("project_id") or "").strip()
                    if _pid:
                        _project_ready.append(a)
                if _project_ready:
                    if len(_project_ready) < len(active):
                        _filelog(
                            f"[Queue] Project-ready filter: {len(_project_ready)}/{len(active)} "
                            f"account có api_session.project_id → using {[a.name for a in _project_ready]}"
                        )
                    active = _project_ready
                else:
                    _filelog(
                        f"[Queue] Project-ready filter: 0/{len(active)} account có api_session.project_id "
                        f"→ fallback active để thử search/create project"
                    )

                for acc in active:
                    if acc.name not in self._cookie_busy_veo:
                        self._cookie_busy_veo[acc.name] = 0

                # ── Ưu tiên tái-claim account đã pick trước đó (giữ 1-task-1-account qua re-enqueue) ──
                # Khi task bị re-enqueue (ví dụ sau một vòng 403-loop), task.picked_account_name
                # đã set sẵn. Không để fair scheduler chọn account khác — ép quay về CÙNG account
                # cũ (chờ slot / chờ cooldown nếu cần). Nếu account cũ đã inactive → fallthrough
                # về logic pick best_acc như thường.
                prev_picked = getattr(task, "picked_account_name", None)
                if prev_picked:
                    prev_acc = next((a for a in active if a.name == prev_picked), None)
                    if prev_acc:
                        prev_cur = busy_dict.get(prev_acc.name, 0)
                        if _is_image_task:
                            prev_max = self._MAX_IMAGE_PER_ACCOUNT
                            _interval = float(getattr(self, "_PER_ACCOUNT_TASK_INTERVAL", 10) or 10)
                        elif _is_video_task:
                            prev_max = self._MAX_VIDEO_PER_ACCOUNT
                            _interval = float(getattr(self, "_PER_ACCOUNT_VIDEO_INTERVAL", 15) or 15)
                        else:
                            prev_max = 999
                            _interval = 0
                        _last_done = float(self._per_account_task_ts.get(prev_acc.name, 0) or 0)
                        _remain = (_last_done + _interval) - time.time()
                        if prev_cur >= prev_max:
                            # Slot đầy → chờ, KHÔNG swap
                            now = time.time()
                            last_log = float(self._cookie_wait_log_ts.get(prev_acc.name, 0) or 0)
                            if now - last_log >= 10:
                                self._cookie_wait_log_ts[prev_acc.name] = now
                                _filelog(
                                    f"[Queue] Task {task.id}: prev account {prev_acc.name} busy "
                                    f"({prev_cur}/{prev_max}) — chờ slot trống (KHÔNG swap)"
                                )
                            self._cookie_cond.wait(timeout=2)
                            continue
                        if _remain > 0:
                            # Cooldown → chờ, KHÔNG swap
                            self._cookie_cond.wait(timeout=min(2, max(0.2, _remain)))
                            continue
                        # Claim lại cùng account
                        busy_dict[prev_acc.name] = prev_cur + 1
                        task.picked_account_name = prev_acc.name
                        try:
                            store.update_video_task(task)
                        except Exception:
                            pass
                        _filelog(
                            f"[Queue-{task_type.upper()}] re-claim account={prev_acc.name} "
                            f"task={getattr(task, 'id', '?')} lanes={busy_dict[prev_acc.name]}/{self._PER_TOKEN_LANES} "
                            "single_chrome_per_token=1 flow=legacy_recovery"
                        )
                        return prev_acc.name
                    # prev_acc không còn active → fallthrough về logic pick best_acc

                if active:
                    # ── Fair scheduler: Least-Busy + Least-Recently-Used tie-break ──
                    # Trước đây: min() theo busy count → khi nhiều account cùng busy=0,
                    #   Python min() luôn trả về first occurrence → account đầu list được
                    #   ưu tiên bias → 1-2 account chạy nhiều hơn hẳn các account khác.
                    # Giờ: tie-break bằng _per_account_task_ts (thời điểm account vừa xong
                    #   task gần nhất) → account rảnh LÂU NHẤT được chọn → round-robin fair.
                    #   Kết quả deterministic, không cần random, debug dễ.
                    best_acc = min(
                        active,
                        key=lambda a: (
                            busy_dict.get(a.name, 0),                              # 1) ít busy nhất
                            float(self._per_account_task_ts.get(a.name, 0) or 0),  # 2) ai rảnh lâu nhất (ts cũ nhất = chờ lâu nhất)
                        ),
                    )
                    current = busy_dict.get(best_acc.name, 0)

                    # ─── Cooldown giữa các task ẢNH trên cùng account ───
                    if _is_image_task:
                        last_done = float(self._per_account_task_ts.get(best_acc.name, 0) or 0)
                        # Interval mặc định cho task ảnh: 10s
                        interval = float(getattr(self, "_PER_ACCOUNT_TASK_INTERVAL", 10) or 10)
                        # Override 15s nếu task trước có upsample (4K/2K) → cho account nghỉ lâu hơn
                        last_upsample = float(self._last_upsample_completed_ts.get(best_acc.name, 0) or 0)
                        _cooldown_reason = "ảnh"
                        if last_upsample > 0 and last_upsample >= last_done - 1:
                            # Upsample happened trong task gần nhất → 15s cooldown
                            interval = float(getattr(self, "_PER_ACCOUNT_UPSAMPLE_INTERVAL", 15) or 15)
                            _cooldown_reason = "ảnh upsample"
                        remain = (last_done + interval) - time.time()
                        if remain > 0:
                            # Rate-limit log cho đỡ loạn (mỗi ~10s/log/account)
                            now = time.time()
                            last_log = float(self._cookie_wait_log_ts.get(best_acc.name, 0) or 0)
                            if now - last_log >= 10:
                                self._cookie_wait_log_ts[best_acc.name] = now
                                _filelog(
                                    f"[Queue] Account {best_acc.name} cooldown {remain:.1f}s — chờ task {_cooldown_reason} tiếp theo"
                                )
                            self._cookie_cond.wait(timeout=min(2, max(0.2, remain)))
                            continue

                    # ─── Cooldown giữa các task VIDEO trên cùng account (15s) ───
                    if _is_video_task:
                        last_done = float(self._per_account_task_ts.get(best_acc.name, 0) or 0)
                        interval = float(getattr(self, "_PER_ACCOUNT_VIDEO_INTERVAL", 15) or 15)
                        remain = (last_done + interval) - time.time()
                        if remain > 0:
                            now = time.time()
                            last_log = float(self._cookie_wait_log_ts.get(best_acc.name, 0) or 0)
                            if now - last_log >= 10:
                                self._cookie_wait_log_ts[best_acc.name] = now
                                _filelog(
                                    f"[Queue] Account {best_acc.name} video cooldown {remain:.1f}s — chờ task video tiếp theo"
                                )
                            self._cookie_cond.wait(timeout=min(2, max(0.2, remain)))
                            continue

                    # ─── Giới hạn cứng: mỗi account tối đa N task ẢNH đồng thời ───
                    # Nếu task là CREATE_IMAGE và account đã đạt ngưỡng → chờ slot trống
                    if _is_image_task and current >= self._MAX_IMAGE_PER_ACCOUNT:
                        now = time.time()
                        last_log = float(self._cookie_wait_log_ts.get(best_acc.name, 0) or 0)
                        if now - last_log >= 10:
                            self._cookie_wait_log_ts[best_acc.name] = now
                            _filelog(
                                f"[Queue] Account {best_acc.name} đã đạt giới hạn "
                                f"{self._MAX_IMAGE_PER_ACCOUNT} task ảnh đồng thời — chờ slot trống"
                            )
                        self._cookie_cond.wait(timeout=2)
                        continue

                    # ─── Giới hạn cứng: mỗi account tối đa 3 lane VIDEO đồng thời ───
                    if _is_video_task and current >= self._MAX_VIDEO_PER_ACCOUNT:
                        now = time.time()
                        last_log = float(self._cookie_wait_log_ts.get(best_acc.name, 0) or 0)
                        if now - last_log >= 10:
                            self._cookie_wait_log_ts[best_acc.name] = now
                            _filelog(
                                f"[Queue] Account {best_acc.name} đã đạt giới hạn "
                                f"{self._MAX_VIDEO_PER_ACCOUNT} task video đồng thời — chờ slot trống"
                            )
                        self._cookie_cond.wait(timeout=2)
                        continue

                    busy_dict[best_acc.name] = current + 1
                    task.picked_account_name = best_acc.name
                    store.update_video_task(task)
                    _filelog(
                        f"[Queue-{task_type.upper()}] claim account={best_acc.name} "
                        f"task={getattr(task, 'id', '?')} lanes={busy_dict[best_acc.name]}/{self._PER_TOKEN_LANES} "
                        "single_chrome_per_token=1 flow=legacy_recovery"
                    )
                    return best_acc.name

                # Nếu không có active account nào thì mới phải chờ
                self._cookie_cond.wait(timeout=2)

    def _release_cookie(self, account_name: str, task_type: str = "veo"):
        """Giảm counter khi task hoàn thành. Notify workers đang chờ."""
        busy_dict = self._cookie_busy_veo
        with self._cookie_cond:
            # Mark thời điểm kết thúc task cho cooldown giữa các task ảnh.
            self._per_account_task_ts[account_name] = time.time()
            cur = busy_dict.get(account_name, 0)
            busy_dict[account_name] = max(0, cur - 1)
            self._cookie_cond.notify_all()  # Đánh thức workers đang chờ

    def _record_403(self, account_name: str, flow: str = ""):
        """Ghi nhận lỗi 403 để theo dõi (counter per account)."""
        if not account_name:
            return
        fails = self._account_403_fails.get(account_name, 0) + 1
        self._account_403_fails[account_name] = fails
        logger.warning(
            f"[{flow}] 403 error trên {account_name} (fails: {fails})"
        )

    def _handle_403_retry(self, account_name: str, flow: str = "") -> "str | None":
        """Xử lý lỗi 403 reCAPTCHA theo policy:

        - Trong 2 phút đầu: CHỈ retry (lấy captcha mới), KHÔNG xoay proxy.
        - Sau 2 phút: Xoay proxy bằng key KiotProxy gán cho account đó.
        - KHÔNG swap account, KHÔNG ban account.

        Returns:
            str  -> chuỗi proxy mới (sau khi hết 2 phút + xoay thành công).
            None -> chưa hết 2 phút (chỉ cần retry captcha) hoặc xoay thất bại.
        """
        if not account_name:
            return None

        _WINDOW = 120  # 2 phút

        now = time.time()
        start_ts = self._account_403_start_ts.get(account_name)

        # Lần 403 đầu tiên → ghi mốc thời gian, retry ngay (không xoay proxy)
        if start_ts is None:
            self._account_403_start_ts[account_name] = now
            self._account_403_fails[account_name] = 1
            _filelog(
                f"[{flow}] 403 reCAPTCHA lần 1 trên {account_name} "
                f"→ bắt đầu window 2 phút, retry ngay (KHÔNG xoay proxy)"
            )
            return None

        elapsed = now - start_ts
        count = self._account_403_fails.get(account_name, 0) + 1
        self._account_403_fails[account_name] = count

        if elapsed < _WINDOW:
            # Còn trong window 2 phút → retry ngay, KHÔNG xoay proxy
            remaining = _WINDOW - elapsed
            if count % 10 == 0:  # Log mỗi 10 lần để không spam
                _filelog(
                    f"[{flow}] 403 #{count} trên {account_name} "
                    f"(còn {remaining:.0f}s trong window) → retry captcha mới"
                )
            return None

        # ── Đã hết 2 phút → xoay proxy bằng key gán cho account ──
        _filelog(
            f"[{flow}] 403 #{count} trên {account_name}: "
            f"HẾT 2 PHÚT ({elapsed:.0f}s, {count} lần fail) → xoay proxy"
        )

        rotated = self._rotate_kiotproxy_key_for_account(account_name)
        # Reset timer cho window mới
        self._account_403_start_ts[account_name] = now
        self._account_403_fails[account_name] = 0

        if rotated:
            _filelog(f"[{flow}] ✅ Proxy mới cho {account_name}: {rotated}")
            return rotated
        else:
            _filelog(f"[{flow}] ⚠️ Xoay proxy thất bại (cooldown), tiếp tục retry với proxy hiện tại")
            return None

    def _account_api_gate(self, account_name: str, flow: str = ""):
        """
        Enforce khoảng cách tối thiểu giữa các API calls trên CÙNG 1 account.
        Quan trọng: KHÔNG sleep khi đang giữ lock, để các accounts khác không bị block theo.
        """
        if not account_name:
            return
        now = time.time()
        wait_s = 0.0
        with self._per_account_api_lock:
            last = float(self._per_account_api_ts.get(account_name, 0) or 0)
            target = max(now, last + float(getattr(self, "_PER_ACCOUNT_MIN_INTERVAL", 0) or 0))
            self._per_account_api_ts[account_name] = target
            wait_s = max(0.0, target - now)
        if wait_s > 0:
            logger.info(
                f"[{flow or 'ApiGate'}] ⏳ Account gate ({account_name[:15]}): chờ {wait_s:.1f}s"
            )
            time.sleep(wait_s)

    def _reset_403(self, account_name: str):
        """Xóa bộ đếm lỗi 403 khi account tạo video/ảnh thành công."""
        if hasattr(self, "_account_403_fails") and account_name in self._account_403_fails:
            if self._account_403_fails[account_name] > 0:
                self._account_403_fails[account_name] = 0
                logger.debug(f"[Reset] 403 counter for {account_name}")

    # ─── Captcha Stats Methods ───

    def _record_captcha_stat(self, account_name: str, source: str, result: str):
        """Ghi nhận thống kê captcha.
        source: 'Remote' | 'Extension'
        result: 'received' | 'pass' | 'fail'
        """
        if not account_name:
            return
        src_key = "remote" if "remote" in source.lower() else "extension"
        stat_key = f"{src_key}_{result}"
        with self._captcha_stats_lock:
            if account_name not in self._captcha_stats:
                self._captcha_stats[account_name] = {
                    "remote_received": 0, "remote_pass": 0, "remote_fail": 0,
                    "extension_received": 0, "extension_pass": 0, "extension_fail": 0,
                }
            if stat_key in self._captcha_stats[account_name]:
                self._captcha_stats[account_name][stat_key] += 1
        if result != "received":
            logger.info(f"[CaptchaStats] {account_name}: {src_key} {result} (total: {self._captcha_stats[account_name][stat_key]})")

    def get_captcha_stats(self) -> dict:
        """Trả về snapshot thống kê captcha (thread-safe copy)."""
        with self._captcha_stats_lock:
            import copy
            return copy.deepcopy(self._captcha_stats)

    def _ban_account(self, account_name: str, reason: str, flow: str = ""):
        """Tắt account (is_active=False) và ghi lý do ban."""
        if not account_name:
            return
        self._ban_reasons[account_name] = reason
        try:
            _bad_acc = store.get_veo_account_by_name(account_name)
            if _bad_acc:
                _bad_acc.is_active = False
                _bad_acc.ban_reason = reason
                store.update_veo_account(_bad_acc)
                _filelog(f"[BAN] Account {account_name} disabled. Reason: {reason} (flow={flow})")
                logger.warning(f"[BAN] {account_name} → is_active=False | Reason: {reason}")
        except Exception as e:
            logger.error(f"[BAN] Failed to ban {account_name}: {e}")

    def get_ban_reasons(self) -> dict:
        """Trả về dict {account_name: reason} cho tất cả account bị ban."""
        return dict(self._ban_reasons)

    def get_proxy_health(self) -> dict:
        """Trả về dict {account_name: 'alive'|'dead'} cho proxy health."""
        return dict(self._proxy_health)



    def _save_proxy_pool(self):
        """Lưu proxy pool vào MongoDB."""
        try:
            store.set_setting("proxy_reserve_pool", list(self._proxy_reserve_pool))
        except Exception as e:
            logger.error(f"[ProxyPool] Failed to save pool to DB: {e}")

    def _save_dead_log(self):
        """Lưu dead proxy log vào MongoDB."""
        try:
            store.set_setting("dead_proxy_log", list(self._dead_proxy_log))
        except Exception as e:
            logger.error(f"[ProxyPool] Failed to save dead log to DB: {e}")

    def _get_dead_proxy_set(self) -> set:
        """Trả về set các proxy URL đã từng bị chặn (normalized lowercase)."""
        return {entry.get("dead_proxy", "").strip().lower() for entry in self._dead_proxy_log if entry.get("dead_proxy")}

    def _filter_against_dead_log(self, proxies: list) -> tuple:
        """Lọc bỏ proxy đã từng bị chặn (có trong dead_proxy_log).
        Returns: (clean_proxies, skipped_count)"""
        dead_set = self._get_dead_proxy_set()
        # Cũng check proxy đang gán cho account nào bị ban
        accounts = store.list_veo_accounts()
        current_proxies = set()
        for acc in accounts:
            p = getattr(acc, "proxy", "") or ""
            if p.strip():
                current_proxies.add(p.strip().lower())

        clean = []
        skipped = 0
        for p in proxies:
            p_lower = p.strip().lower()
            if p_lower in dead_set:
                skipped += 1
                logger.info(f"[ProxyPool] Skipped dead proxy: {p[:50]}")
            elif p_lower in current_proxies:
                skipped += 1
                logger.info(f"[ProxyPool] Skipped proxy already assigned: {p[:50]}")
            else:
                clean.append(p)
        return clean, skipped

    @staticmethod
    def check_proxy_live(proxy_url: str, timeout: float = 5.0) -> bool:
        """Kiểm tra proxy có live không bằng TCP connect.
        Hỗ trợ format: http://user:pass@host:port"""
        import socket
        try:
            from urllib.parse import urlparse
            parsed = urlparse(proxy_url)
            host = parsed.hostname
            port = parsed.port or 8080
            if not host:
                return False
            sock = socket.create_connection((host, port), timeout=timeout)
            sock.close()
            return True
        except Exception:
            return False

    def validate_and_set_proxy_pool(self, proxies: list, mode: str = "replace", check_live: bool = True) -> dict:
        """Validate proxies (lọc dead + check live) rồi add vào pool.
        Returns: {added, skipped_dead, skipped_offline, total}"""
        # 1. Lọc proxy đã từng bị chặn
        clean, skipped_dead = self._filter_against_dead_log(proxies)

        # 2. Check live (TCP connect)
        skipped_offline = 0
        live_proxies = []
        if check_live:
            for p in clean:
                if self.check_proxy_live(p, timeout=5.0):
                    live_proxies.append(p)
                else:
                    skipped_offline += 1
                    logger.warning(f"[ProxyPool] Proxy OFFLINE (TCP fail): {p[:50]}")
        else:
            live_proxies = clean

        # 3. Add vào pool
        with self._proxy_reserve_lock:
            if mode == "append":
                self._proxy_reserve_pool.extend(live_proxies)
            else:
                self._proxy_reserve_pool = list(live_proxies)

        # 4. Lưu vào DB
        self._save_proxy_pool()

        total = len(self._proxy_reserve_pool)
        logger.info(
            f"[ProxyPool] Validate done: added={len(live_proxies)}, "
            f"skipped_dead={skipped_dead}, skipped_offline={skipped_offline}, pool_total={total}"
        )
        return {
            "added": len(live_proxies),
            "skipped_dead": skipped_dead,
            "skipped_offline": skipped_offline,
            "total": total,
        }

    def set_proxy_reserve_pool(self, proxies: list):
        """Set danh sách proxy dự phòng (thay thế pool cũ) — KHÔNG validate."""
        with self._proxy_reserve_lock:
            self._proxy_reserve_pool = list(proxies)
        self._save_proxy_pool()
        logger.info(f"[ProxyPool] Reserve pool set: {len(proxies)} proxies")

    def add_to_proxy_reserve_pool(self, proxies: list):
        """Thêm proxy vào pool (append, không thay thế) — KHÔNG validate."""
        with self._proxy_reserve_lock:
            self._proxy_reserve_pool.extend(proxies)
        self._save_proxy_pool()
        logger.info(f"[ProxyPool] Added {len(proxies)} proxies, total now: {len(self._proxy_reserve_pool)}")

    def get_proxy_reserve_pool(self) -> list:
        """Trả về danh sách proxy còn trong reserve pool."""
        with self._proxy_reserve_lock:
            return list(self._proxy_reserve_pool)

    def get_dead_proxy_log(self) -> list:
        """Trả về lịch sử proxy chết (bị IP_FILTER)."""
        return list(self._dead_proxy_log)

    def clear_proxy_reserve_pool(self):
        """Xoá toàn bộ reserve pool."""
        with self._proxy_reserve_lock:
            count = len(self._proxy_reserve_pool)
            self._proxy_reserve_pool.clear()
        self._save_proxy_pool()
        logger.info(f"[ProxyPool] Cleared reserve pool ({count} proxies removed)")
        return count

    # ── Upload-retry helpers (rotate proxy_pool khi upload fail) ──────────────
    # Wrapper CHỈ rotate khi gặp proxy err. Lỗi khác (auth / bad_request /
    # server / rate_limit / unknown) trả về cho caller xử lý theo logic cũ —
    # KHÔNG đổi behavior so với trước khi có wrapper.
    _UPLOAD_PROXY_RETRY_SLEEP = 1.0   # sleep giữa các lần rotate

    @staticmethod
    def _classify_upload_error(status) -> str:
        """Phân loại lỗi upload từ HTTP status code.
        status = None → coi như exception/network (giả định proxy err)."""
        if status is None:
            return "proxy"
        try:
            s = int(status)
        except Exception:
            return "unknown"
        if s == 200:
            return "ok"
        if s == 401:
            return "auth"
        if s in (400, 413, 415):
            return "bad_request"
        if s == 429:
            return "rate_limit"
        if 500 <= s < 600:
            return "server"
        return "unknown"

    def _upload_with_proxy_retry(
        self,
        client,
        upload_fn,
        *args,
        flow: str = "?",
        task_id: str = "?",
        **kwargs,
    ):
        """Wrap 1 upload call với auto-rotate proxy_pool KHI VÀ CHỈ KHI proxy err.

        - Proxy err → mark dead + pick fresh proxy + retry. Vô hạn cho tới khi
          OK hoặc pool exhausted.
        - Mọi lỗi khác (auth / bad_request / server / rate_limit / unknown):
          trả về NGAY cho caller, KHÔNG retry, KHÔNG rotate. Caller xử lý theo
          logic cũ.
        - Khi upload OK với proxy mới → giữ luôn proxy đó trên client.

        Returns: (result, err_class)
            result: media_id nếu OK, None nếu fail.
            err_class: 'ok' / 'proxy_exhausted' / 'auth' / 'bad_request' /
                       'server' / 'rate_limit' / 'unknown'
        """
        if getattr(self, "disable_media_proxy", True):
            try:
                result = upload_fn(*args, **kwargs)
            except Exception:
                result = None
            return (result, "ok") if result else (None, self._classify_upload_error(getattr(client, "_last_upload_status", None)))

        from core.static_proxy_pool import (
            get_fresh_proxy,
            mark_proxy_dead,
            is_pool_exhausted,
            pool_size,
            dead_count,
        )

        rotate_count = 0

        while True:
            # Reset status để biết status của lần gọi này
            try:
                client._last_upload_status = None
            except Exception:
                pass

            old_proxy = getattr(client, "proxy", None)
            exception_msg = None
            try:
                result = upload_fn(*args, **kwargs)
            except Exception as e:
                result = None
                exception_msg = str(e)[:200]

            if result:
                if rotate_count > 0:
                    _filelog(
                        f"[Upload-OK] {flow} task={task_id} after {rotate_count} rotate(s) "
                        f"final_proxy={(old_proxy or 'DIRECT')[:40]}"
                    )
                return result, "ok"

            status = getattr(client, "_last_upload_status", None)
            err_class = self._classify_upload_error(status)

            # ── KHÔNG phải proxy err → trả ngay cho caller (giữ logic cũ) ──
            if err_class != "proxy":
                if rotate_count > 0:
                    _filelog(
                        f"[Upload-NonProxy] {flow} task={task_id} status={status} "
                        f"err={err_class} after {rotate_count} rotate(s) → return to caller"
                    )
                return None, err_class

            # ── proxy err → rotate ──
            if old_proxy:
                mark_proxy_dead(old_proxy)
                _filelog(
                    f"[Upload-Dead] {flow} task={task_id} proxy={old_proxy[:40]} "
                    f"reason=proxy_err exc={exception_msg or 'status_'+str(status)}"
                )

            if is_pool_exhausted():
                _filelog(
                    f"[Upload-Exhausted] {flow} task={task_id} "
                    f"dead={dead_count()}/{pool_size()} → return"
                )
                return None, "proxy_exhausted"

            new_proxy = get_fresh_proxy()
            if new_proxy is None:
                _filelog(
                    f"[Upload-Exhausted] {flow} task={task_id} no_fresh_proxy → return"
                )
                return None, "proxy_exhausted"

            try:
                client.proxy = new_proxy
            except Exception as set_err:
                _filelog(
                    f"[Upload-Rotate] {flow} task={task_id} cannot set proxy: {set_err} → return"
                )
                return None, "unknown"

            rotate_count += 1
            _filelog(
                f"[Upload-Rotate] {flow} task={task_id} proxy "
                f"{(old_proxy or 'DIRECT')[:40]} → {new_proxy[:40]} "
                f"(rotate#{rotate_count}, dead={dead_count()}/{pool_size()})"
            )
            time.sleep(self._UPLOAD_PROXY_RETRY_SLEEP)

    def _auto_replace_proxy(self, account_name: str) -> "str | None":
        """Lấy 1 proxy mới từ reserve pool, gán cho account, gỡ ban, ghi log proxy chết.
        Skip proxy đã từng bị chặn. Returns: proxy mới nếu thành công, None nếu pool rỗng."""
        import datetime

        # Lấy account từ DB trước
        acc = store.get_veo_account_by_name(account_name)
        if not acc:
            logger.error(f"[ProxyPool] Account {account_name} not found in DB")
            return None

        old_proxy = getattr(acc, "proxy", "") or ""
        dead_set = self._get_dead_proxy_set()

        # Pop proxy từ pool, skip proxy đã từng chết
        new_proxy = None
        skipped = 0
        with self._proxy_reserve_lock:
            while self._proxy_reserve_pool:
                candidate = self._proxy_reserve_pool.pop(0)
                if candidate.strip().lower() in dead_set:
                    skipped += 1
                    logger.info(f"[ProxyPool] Skipped previously dead proxy: {candidate[:50]}")
                    continue
                new_proxy = candidate
                break

        if not new_proxy:
            if skipped:
                logger.warning(f"[ProxyPool] Pool exhausted after skipping {skipped} dead proxies")
            return None

        if skipped:
            logger.info(f"[ProxyPool] Skipped {skipped} dead proxies before finding a good one")

        # Ghi log proxy chết
        self._dead_proxy_log.append({
            "dead_proxy": old_proxy,
            "account": account_name,
            "reason": "IP_FILTER — Google chặn IP proxy",
            "replaced_with": new_proxy,
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

        # Gán proxy mới cho account
        acc.proxy = new_proxy
        acc.is_active = True
        acc.ban_reason = None
        store.update_veo_account(acc)

        # Xoá ban reason in-memory
        self._ban_reasons.pop(account_name, None)

        # Xoá proxy health cũ
        self._proxy_health.pop(account_name, None)

        _filelog(
            f"[ProxyPool] Replaced proxy: {account_name} | "
            f"old={old_proxy[:40] if old_proxy else 'none'} → new={new_proxy[:40]} | "
            f"pool_remaining={len(self._proxy_reserve_pool)}"
        )

        # Lưu thay đổi vào DB
        self._save_proxy_pool()
        self._save_dead_log()

        # Đánh thức workers đang chờ slot account (vì account vừa được gỡ ban)
        with self._cookie_cond:
            self._cookie_cond.notify_all()

        return new_proxy

    def get_ip_error_counts(self) -> dict:
        """Trả về dict {account_name: int} — số lần bị Google reject vì IP proxy."""
        return dict(self._account_ip_errors)

    def _record_ip_error(self, account_name: str) -> "str | None":
        """Ghi nhận lỗi IP_FILTER/IP_INPUT → auto-rotate KiotProxy.
        KHÔNG BAN account — chỉ đổi proxy IP rồi tiếp tục.
        Trả về proxy mới nếu xoay thành công, ngược lại None."""
        if not account_name:
            return None
        count = self._account_ip_errors.get(account_name, 0) + 1
        self._account_ip_errors[account_name] = count
        logger.warning(
            f"[IPError] Google reject IP trên {account_name} (lần {count}) — proxy disabled, giữ DIRECT"
        )
        _filelog(f"[IPError] {account_name}: media proxy disabled → no rotate, continue DIRECT")
        return None

    def _wait_for_any_proxy_available(self, flow: str = "") -> float:
        """Kiểm tra tất cả proxy key — nếu TẤT CẢ đều đang cooldown thì chờ key gần nhất.
        Trả về 0 nếu có key sẵn sàng, hoặc số giây đã chờ."""
        keys = self._get_kiotproxy_pool_keys()
        if not keys:
            return 0
        min_remain = float("inf")
        all_in_cd = True
        for k in keys:
            in_cd, remain = self._is_rotate_in_cooldown(k)
            if not in_cd:
                all_in_cd = False
                break
            min_remain = min(min_remain, remain)
        if all_in_cd and min_remain > 0 and min_remain < 300:
            wait_sec = min(min_remain + 2, 130)  # chờ thêm 2s buffer, tối đa 130s
            _filelog(
                f"[{flow}] ⏳ TẤT CẢ {len(keys)} proxy key đều cooldown — "
                f"chờ {wait_sec:.0f}s cho key gần nhất hết hạn (KHÔNG spam swap)"
            )
            time.sleep(wait_sec)
            return wait_sec
        return 0

    def _handle_unusual_activity(self, account_name: str, flow: str = "") -> bool:
        """Xử lý UNUSUAL_ACTIVITY: giữ nguyên account, retry liên tục trong 2 phút.
        
        Logic (v3 — time-window):
        - Lần 403 đầu tiên → bắt đầu đếm 2 phút.
        - Retry liên tục mỗi 5s trên CÙNG account.
        - Hết 2 phút vẫn 403 → CANCEL task, trả ảnh gốc (KHÔNG swap).
        
        Returns: True = hết thời gian, cancel task. False = retry cùng account (chờ 5s).
        """
        if not account_name:
            return False
        
        _WINDOW = 120  # 2 phút
        
        now = time.time()
        count = self._unusual_activity_counts.get(account_name, 0) + 1
        self._unusual_activity_counts[account_name] = count
        
        # Lần đầu → ghi mốc thời gian bắt đầu
        if account_name not in self._unusual_activity_start_ts:
            self._unusual_activity_start_ts[account_name] = now
        
        start_ts = self._unusual_activity_start_ts[account_name]
        elapsed = now - start_ts
        remaining = _WINDOW - elapsed
        if remaining > 0:
            logger.warning(
                f"[{flow}] UNUSUAL_ACTIVITY #{count} trên {account_name} "
                f"→ proxy disabled, chờ 10s rồi retry DIRECT (còn {remaining:.0f}s trong window)"
            )
            time.sleep(10)
            return False  # Retry tiếp
        else:
            logger.warning(
                f"[{flow}] UNUSUAL_ACTIVITY #{count} trên {account_name} "
                f"→ hết window {elapsed:.0f}s nhưng proxy disabled, tiếp tục DIRECT"
            )
            self._unusual_activity_start_ts[account_name] = now
            self._unusual_activity_counts[account_name] = 0
            time.sleep(5)
            return False  # Retry tiếp



    # ─── Real-time Activity Tracker Methods ───

    def _set_activity(self, account_name: str, phase: str, task_id: str = ""):
        """Cập nhật trạng thái phase hiện tại cho account (thread-safe)."""
        if not account_name:
            return
        with self._activity_lock:
            self._account_activity[account_name] = {
                "phase": phase,
                "task_id": str(task_id) if task_id else "",
                "since": time.time(),
            }

    def _clear_activity(self, account_name: str):
        """Xóa trạng thái khi task hoàn thành."""
        if not account_name:
            return
        with self._activity_lock:
            self._account_activity.pop(account_name, None)
        # Reset 403 time-window tracker
        self._account_403_start_ts.pop(account_name, None)
        self._account_403_fails.pop(account_name, None)

    def _cleanup_stale_activities(self):
        """Tự động dọn activities bị stuck.
        - RESOLVING_COOKIE, SOLVING_CAPTCHA: cleanup sau 60s (thường xong trong vài giây)
        - Các phase khác (trừ POLLING_RESULT, DOWNLOADING): cleanup sau ACTIVITY_MAX_AGE (300s)
        - POLLING_RESULT, DOWNLOADING: không auto-clear (có thể kéo dài 10 phút hợp lệ)"""
        now = time.time()
        # Các phase cho phép chạy lâu — không auto-clear
        _LONG_RUNNING_PHASES = {"POLLING_RESULT", "DOWNLOADING"}
        # Các phase phải xong nhanh — cleanup sớm hơn (60s thay vì 300s)
        _QUICK_TIMEOUT_PHASES = {"RESOLVING_COOKIE", "SOLVING_CAPTCHA"}
        _QUICK_TIMEOUT = 60  # giây
        with self._activity_lock:
            stale = [
                name for name, info in self._account_activity.items()
                if info.get("phase") not in _LONG_RUNNING_PHASES
                and (
                    (info.get("phase") in _QUICK_TIMEOUT_PHASES and now - info.get("since", now) > _QUICK_TIMEOUT)
                    or now - info.get("since", now) > ACTIVITY_MAX_AGE
                )
            ]
            for name in stale:
                logger.warning(
                    f"[Activity] Auto-clearing stale activity for {name} "
                    f"(phase={self._account_activity[name].get('phase')}, "
                    f"stuck > {_QUICK_TIMEOUT if self._account_activity[name].get('phase') in _QUICK_TIMEOUT_PHASES else ACTIVITY_MAX_AGE}s)"
                )
                del self._account_activity[name]

    def get_account_activities(self) -> dict:
        """Snapshot trạng thái tất cả accounts (thread-safe copy)."""
        with self._activity_lock:
            return dict(self._account_activity)

    # ─── Client Resolution ───

    @staticmethod
    def _normalize_proxy_url(raw: str) -> "str | None":
        """Normalize proxy string: host:port:user:pass → http://user:pass@host:port."""
        if not raw:
            return None
        s = raw.strip()
        if not s:
            return None
        # Đã có scheme → giữ nguyên
        lower = s.lower()
        for scheme in ("http://", "https://", "socks5://", "socks4://"):
            if lower.startswith(scheme):
                return s
        parts = s.split(":")
        if len(parts) == 4:
            host, port, user, password = parts
            return f"http://{user}:{password}@{host}:{port}"
        if "@" in s:
            return f"http://{s}"
        if len(parts) == 2:
            return f"http://{s}"
        # Chuỗi không chứa ':' hoặc '@' → KHÔNG phải proxy URL hợp lệ
        # (VD: KiotProxy API key k41dd92e... sẽ bị reject ở đây)
        _filelog(f"[Proxy] _normalize_proxy_url: rejected invalid proxy string: {s[:40]}")
        return None

    # ─── KiotProxy API Integration ───

    def _fetch_kiotproxy(self, endpoint: str = "current") -> "dict | None":
        """Gọi KiotProxy API để lấy proxy hiện tại hoặc xoay proxy mới.
        endpoint: 'current' hoặc 'new'
        Trả về dict proxy data hoặc None."""
        if getattr(self, "disable_media_proxy", True):
            return None
        import requests as _req
        key = ""
        try:
            key = store.get_setting("kiotproxy_key", "")
        except Exception:
            pass
        if not key or not key.strip():
            return None

        url = f"https://api.kiotproxy.com/api/v1/proxies/{endpoint}"
        params = {"key": key.strip()}
        # Nếu gọi 'new', thêm region
        if endpoint == "new":
            region = "random"
            try:
                region = store.get_setting("kiotproxy_region", "random") or "random"
            except Exception:
                pass
            params["region"] = region

        try:
            resp = _req.get(url, params=params, timeout=15)
            jr = resp.json()
            if jr.get("success") and jr.get("data"):
                data = jr["data"]
                data["fetched_at"] = time.time()
                # Cache
                with self._kiotproxy_lock:
                    self._kiotproxy_cache = data
                    self._kiotproxy_last_fetch = time.time()
                # Success → clear cooldown legacy key
                with self._kiotproxy_rotate_lock:
                    self._kiotproxy_rotate_cooldown_until.pop(key.strip(), None)
                _filelog(
                    f"[KiotProxy] {endpoint.upper()} OK → "
                    f"http={data.get('http')} location={data.get('location')} "
                    f"ttl={data.get('ttl')}s ttc={data.get('ttc')}s"
                )
                return data
            else:
                err = jr.get("message") or jr.get("error") or "Unknown error"
                err_str = str(err)
                _filelog(f"[KiotProxy] {endpoint.upper()} FAILED: {err}")
                # Set local cooldown cho legacy key để tránh spam /new
                if endpoint == "new":
                    cooldown_sec = self._KIOTPROXY_ROTATE_DEFAULT_COOLDOWN
                    try:
                        import re as _re_cd2
                        m = _re_cd2.search(r"sau\s+(\d+)\s*gi[aâ]y", err_str, _re_cd2.IGNORECASE)
                        if m:
                            cooldown_sec = max(1, int(m.group(1)))
                        elif "quá nhiều lần" in err_str.lower() or "rate" in err_str.lower():
                            cooldown_sec = self._KIOTPROXY_ROTATE_RATELIMIT_COOLDOWN
                    except Exception:
                        pass
                    with self._kiotproxy_rotate_lock:
                        self._kiotproxy_rotate_cooldown_until[key.strip()] = time.time() + cooldown_sec
                    _filelog(
                        f"[KiotProxy] legacy key cooldown {cooldown_sec}s (next retry allowed)"
                    )
                return None
        except Exception as e:
            _filelog(f"[KiotProxy] {endpoint.upper()} EXCEPTION: {e}")
            return None

    def get_kiotproxy_current(self) -> "dict | None":
        """Lấy proxy hiện tại từ KiotProxy API (có cache TTL-aware).
        Auto-fallback: nếu /current fail (chưa có proxy) → tự gọi /new."""
        with self._kiotproxy_lock:
            cache = self._kiotproxy_cache
        if cache:
            fetched_at = cache.get("fetched_at", 0)
            ttl = cache.get("ttl", 0)
            age = time.time() - fetched_at
            # Nếu cache vẫn còn hạn → trả về luôn
            if age < ttl and cache.get("http"):
                return cache
        # Cache hết hạn hoặc chưa có → gọi API current
        result = self._fetch_kiotproxy("current")
        if result:
            return result
        # /current fail (PROXY_NOT_FOUND_BY_KEY) → tự gọi /new để lấy proxy mới
        _filelog("[KiotProxy] /current failed → auto-calling /new to get first proxy")
        return self._fetch_kiotproxy("new")

    def rotate_kiotproxy(self) -> "dict | None":
        """Force rotate proxy mới qua KiotProxy API (legacy single-key).
        Tôn trọng local cooldown — không spam /new khi API đã rate-limit."""
        try:
            key = (store.get_setting("kiotproxy_key", "") or "").strip()
        except Exception:
            key = ""
        if key:
            in_cd, remain = self._is_rotate_in_cooldown(key)
            if in_cd:
                _filelog(
                    f"[KiotProxy] rotate_kiotproxy SKIP — còn cooldown {remain:.0f}s, "
                    f"dùng proxy cached hiện tại"
                )
                # Trả về cache hiện tại nếu có, để caller không coi là fail
                with self._kiotproxy_lock:
                    return dict(self._kiotproxy_cache) if self._kiotproxy_cache else None
        return self._fetch_kiotproxy("new")

    def get_kiotproxy_status(self) -> dict:
        """Trả về trạng thái KiotProxy cho UI."""
        key = ""
        try:
            key = store.get_setting("kiotproxy_key", "")
        except Exception:
            pass
        region = "random"
        try:
            region = store.get_setting("kiotproxy_region", "random") or "random"
        except Exception:
            pass
        with self._kiotproxy_lock:
            cache = dict(self._kiotproxy_cache) if self._kiotproxy_cache else None
        result = {
            "key_configured": bool(key and key.strip()),
            "key_preview": (key[:8] + "..." + key[-4:]) if key and len(key) > 12 else key,
            "region": region,
            "current_proxy": None,
        }
        if cache and cache.get("http"):
            fetched_at = cache.get("fetched_at", 0)
            ttl = cache.get("ttl", 0)
            age = time.time() - fetched_at
            result["current_proxy"] = {
                "http": cache.get("http"),
                "socks5": cache.get("socks5"),
                "host": cache.get("host"),
                "location": cache.get("location"),
                "ttl": ttl,
                "ttc": max(0, int(cache.get("ttc", 0) - age)),
                "realIpAddress": cache.get("realIpAddress"),
                "expired": age >= ttl,
            }
        return result

    # ─── KiotProxy POOL (N key / M account per key) ───

    def _get_kiotproxy_pool_keys(self) -> list:
        """Đọc danh sách key từ setting `kiotproxy_keys` (JSON list).
        Fallback: nếu pool rỗng mà có `kiotproxy_key` cũ → dùng key đơn đó."""
        try:
            raw = store.get_setting("kiotproxy_keys", None)
        except Exception:
            raw = None
        keys: list = []
        if isinstance(raw, list):
            keys = [str(k).strip() for k in raw if str(k or "").strip()]
        elif isinstance(raw, str) and raw.strip():
            # Chấp nhận string nhiều dòng nếu migrator chưa chạy
            keys = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        if not keys:
            try:
                legacy = store.get_setting("kiotproxy_key", "")
                if legacy and str(legacy).strip():
                    keys = [str(legacy).strip()]
            except Exception:
                pass
        return keys

    def _get_kiotproxy_accounts_per_key(self) -> int:
        """Số account tối đa share 1 key (M). Mặc định 1."""
        try:
            m = int(store.get_setting("kiotproxy_accounts_per_key", 1) or 1)
        except Exception:
            m = 1
        return max(1, m)

    def _get_kiotproxy_region(self) -> str:
        try:
            r = store.get_setting("kiotproxy_region", "random") or "random"
        except Exception:
            r = "random"
        return str(r).strip() or "random"

    def _fetch_kiotproxy_for_key(self, key: str, endpoint: str = "new") -> "dict | None":
        """Gọi KiotProxy API cho 1 key cụ thể, lưu vào pool cache.
        endpoint: 'new' (xoay) | 'current' (lấy hiện tại).
        Trả về dict data (có thêm 'key','fetched_at','error') hoặc None khi exception mạng."""
        if getattr(self, "disable_media_proxy", True):
            return None
        import requests as _req
        key = (key or "").strip()
        if not key:
            return None
        url = f"https://api.kiotproxy.com/api/v1/proxies/{endpoint}"
        params = {"key": key}
        if endpoint == "new":
            params["region"] = self._get_kiotproxy_region()
        try:
            resp = _req.get(url, params=params, timeout=15)
            jr = resp.json()
        except Exception as e:
            _filelog(f"[KiotProxy-Pool] {endpoint} key={key[:6]}... EXCEPTION: {e}")
            with self._kiotproxy_pool_lock:
                prev = self._kiotproxy_pool_cache.get(key, {})
                prev["error"] = str(e)[:160]
                prev["fetched_at_err"] = time.time()
                self._kiotproxy_pool_cache[key] = prev
            return None

        if jr.get("success") and jr.get("data"):
            data = dict(jr["data"])
            data["key"] = key
            data["fetched_at"] = time.time()
            data["error"] = None
            with self._kiotproxy_pool_lock:
                self._kiotproxy_pool_cache[key] = data
            # Rotate thành công → clear local cooldown cho key này
            with self._kiotproxy_rotate_lock:
                self._kiotproxy_rotate_cooldown_until.pop(key, None)
            _filelog(
                f"[KiotProxy-Pool] {endpoint.upper()} key={key[:6]}... OK "
                f"http={data.get('http')} loc={data.get('location')} "
                f"ttl={data.get('ttl')}s ttc={data.get('ttc')}s"
            )
            return data
        # Lỗi logic (key invalid, TTC chưa tới hạn, rate-limit, ...)
        err = jr.get("message") or jr.get("error") or "Unknown error"
        err_str = str(err)
        _filelog(f"[KiotProxy-Pool] {endpoint} key={key[:6]}... FAIL: {err}")
        with self._kiotproxy_pool_lock:
            prev = self._kiotproxy_pool_cache.get(key, {})
            prev["error"] = err_str[:160]
            prev["fetched_at_err"] = time.time()
            # KHÔNG xoá `http` cũ — giữ proxy hiện tại nếu chỉ lỗi TTC
            self._kiotproxy_pool_cache[key] = prev

        # ── Set local cooldown để tránh spam /new ──
        # Ưu tiên parse "Gửi lại sau N giây" từ message → tôn trọng TTC server-side.
        # Khi gặp rate-limit ("quá nhiều lần") → cooldown rate-limit mặc định.
        # Các lỗi khác → default cooldown 60s (1 phút, user yêu cầu).
        now = time.time()
        cooldown_sec = self._KIOTPROXY_ROTATE_DEFAULT_COOLDOWN
        try:
            import re as _re_cd
            m = _re_cd.search(r"sau\s+(\d+)\s*gi[aâ]y", err_str, _re_cd.IGNORECASE)
            if m:
                cooldown_sec = max(1, int(m.group(1)))
            elif "quá nhiều lần" in err_str.lower() or "rate" in err_str.lower():
                cooldown_sec = self._KIOTPROXY_ROTATE_RATELIMIT_COOLDOWN
        except Exception:
            pass
        with self._kiotproxy_rotate_lock:
            self._kiotproxy_rotate_cooldown_until[key] = now + cooldown_sec
        _filelog(
            f"[KiotProxy-Pool] key={key[:6]}... cooldown {cooldown_sec}s "
            f"(next retry at {int(now + cooldown_sec)})"
        )
        return None

    def _is_rotate_in_cooldown(self, key: str) -> "tuple[bool, float]":
        """Kiểm tra key có đang trong local rotate cooldown không.
        Trả (is_cooldown, remain_sec)."""
        with self._kiotproxy_rotate_lock:
            until = float(self._kiotproxy_rotate_cooldown_until.get(key, 0) or 0)
        now = time.time()
        if until > now:
            return True, until - now
        return False, 0.0

    def refresh_kiotproxy_pool(self, force: bool = False) -> dict:
        """Gọi /new cho các key cần refresh. force=True → bỏ qua TTC, gọi tất cả.
        Tôn trọng local rotate cooldown (trừ khi force=True AND manual_override=True
        — hiện tại force=True cũng tôn trọng cooldown để tránh spam từ thread nền).
        Trả về summary: {"fetched":N, "skipped":M, "cooldown":C, "errors":X, "keys":len}."""
        keys = self._get_kiotproxy_pool_keys()
        now = time.time()
        fetched = 0
        skipped = 0
        cooldown_skipped = 0
        errors = 0
        for k in keys:
            # ── Skip nếu còn trong local cooldown (tránh spam /new) ──
            in_cd, remain = self._is_rotate_in_cooldown(k)
            if in_cd:
                cooldown_skipped += 1
                _filelog(
                    f"[KiotProxy-Pool] key={k[:6]}... SKIP rotate — còn cooldown {remain:.0f}s"
                )
                continue
            with self._kiotproxy_pool_lock:
                cache = dict(self._kiotproxy_pool_cache.get(k, {}))
            # Nếu không force → tôn trọng TTC: gọi khi fetched_at + ttc <= now
            if not force and cache.get("http"):
                ttc = float(cache.get("ttc", 0) or 0)
                fa = float(cache.get("fetched_at", 0) or 0)
                if fa + ttc > now:
                    skipped += 1
                    continue
            res = self._fetch_kiotproxy_for_key(k, endpoint="new")
            if res:
                fetched += 1
            else:
                errors += 1
        return {
            "keys": len(keys),
            "fetched": fetched,
            "skipped": skipped,
            "cooldown_skipped": cooldown_skipped,
            "errors": errors,
        }

    def _kiotproxy_pool_refresh_loop(self):
        """Background daemon: khi start → fetch all; sau đó mỗi TICK giây check TTC và rotate."""
        if getattr(self, "disable_media_proxy", True):
            _filelog("[KiotProxy-Pool] Refresh loop disabled — media runtime runs DIRECT")
            return
        import time as _t
        # Fetch đầu tiên: cho DB settings load xong
        _t.sleep(3)
        try:
            summary = self.refresh_kiotproxy_pool(force=True)
            _filelog(f"[KiotProxy-Pool] Startup fetch: {summary}")
        except Exception as e:
            _filelog(f"[KiotProxy-Pool] Startup fetch error: {e}")

        while True:
            try:
                _t.sleep(self._KIOTPROXY_POOL_REFRESH_TICK)
                summary = self.refresh_kiotproxy_pool(force=False)
                if summary.get("fetched", 0) + summary.get("errors", 0) > 0:
                    _filelog(f"[KiotProxy-Pool] Tick refresh: {summary}")
            except Exception as e:
                _filelog(f"[KiotProxy-Pool] Loop error: {e}")

    def _get_kiotproxy_excluded_accounts(self) -> set:
        """Tên account user đã bỏ chọn (×) trên UI KiotProxy Pool.
        Các account trong set này KHÔNG được gán key nào → fallback direct proxy.
        User có thể bật lại bất cứ lúc nào qua endpoint toggle."""
        try:
            raw = store.get_setting("kiotproxy_excluded_accounts", None)
        except Exception:
            raw = None
        if isinstance(raw, list):
            return {str(n).strip() for n in raw if str(n or "").strip()}
        return set()

    def get_kiotproxy_pool_status(self) -> dict:
        """Trả về trạng thái pool cho UI: danh sách key + proxy hiện tại + account đang gán."""
        keys = self._get_kiotproxy_pool_keys()
        m = self._get_kiotproxy_accounts_per_key()
        region = self._get_kiotproxy_region()
        excluded = self._get_kiotproxy_excluded_accounts()
        # Lấy danh sách active account, bỏ qua account đã bị user exclude (sort by name)
        try:
            accounts = [
                a for a in store.list_veo_accounts()
                if getattr(a, "is_active", False)
                and getattr(a, "name", "") not in excluded
            ]
        except Exception:
            accounts = []
        accounts_sorted = sorted(accounts, key=lambda a: (getattr(a, "name", "") or "").lower())
        now = time.time()
        items = []
        for idx, k in enumerate(keys):
            with self._kiotproxy_pool_lock:
                cache = dict(self._kiotproxy_pool_cache.get(k, {}))
            http = cache.get("http")
            ttl = float(cache.get("ttl", 0) or 0)
            fa = float(cache.get("fetched_at", 0) or 0)
            age = now - fa if fa else 0
            ttc_left = max(0, int(cache.get("ttc", 0) or 0) - int(age))
            # Account gán theo mapping idx*M .. (idx+1)*M - 1
            start = idx * m
            end = min(start + m, len(accounts_sorted))
            assigned = [getattr(a, "name", "") for a in accounts_sorted[start:end]]
            items.append({
                "key_preview": (k[:8] + "..." + k[-4:]) if len(k) > 12 else k,
                "key_hash": hashlib.md5(k.encode()).hexdigest()[:8] if k else "",
                "http": http,
                "host": cache.get("host"),
                "location": cache.get("location"),
                "ttl": int(ttl),
                "ttc_left": ttc_left,
                "expired": bool(fa and age >= ttl) if ttl else False,
                "error": cache.get("error"),
                "fetched_at": int(fa) if fa else 0,
                "assigned_accounts": assigned,
                "assigned_count": len(assigned),
            })
        # Account dư không được gán key (nếu N*M < số account)
        max_slots = len(keys) * m
        unassigned = [getattr(a, "name", "") for a in accounts_sorted[max_slots:]] if max_slots > 0 else []
        return {
            "keys_count": len(keys),
            "accounts_per_key": m,
            "region": region,
            "total_active_accounts": len(accounts_sorted),
            "max_slots": max_slots,
            "unassigned_accounts": unassigned,
            "excluded_accounts": sorted(excluded),
            "items": items,
        }

    def _rotate_kiotproxy_key_for_account(self, acc_name: str) -> "str | None":
        """Gọi /new lên KiotProxy để rotate IP của key đang gán cho account này.
        Dùng khi gặp ProxyError (proxy IP hiện tại đã chết hoặc bị từ chối).
        Trả về proxy URL mới (http://ip:port) hoặc None nếu không rotate được."""
        if getattr(self, "disable_media_proxy", True):
            return None
        if not acc_name:
            return None
        keys = self._get_kiotproxy_pool_keys()
        if not keys:
            return None
        m = self._get_kiotproxy_accounts_per_key()
        excluded = self._get_kiotproxy_excluded_accounts()
        if acc_name in excluded:
            return None
        try:
            accounts = [
                a for a in store.list_veo_accounts()
                if getattr(a, "is_active", False)
                and getattr(a, "name", "") not in excluded
            ]
        except Exception:
            return None
        accounts_sorted = sorted(accounts, key=lambda a: (getattr(a, "name", "") or "").lower())
        try:
            idx = next(i for i, a in enumerate(accounts_sorted) if getattr(a, "name", "") == acc_name)
        except StopIteration:
            return None
        key_idx = idx // m
        if key_idx >= len(keys):
            return None
        k = keys[key_idx]

        # ── Tôn trọng local cooldown: không spam /new khi API đang rate-limit
        #    hoặc proxy chưa tới hạn đổi (KiotProxy limit ~2 phút/key). ──
        in_cd, remain = self._is_rotate_in_cooldown(k)
        if in_cd:
            # Proxy hiện tại trong cache vừa gặp ProxyError → coi là DEAD.
            # XÓA `http` trong cache để _get_kiotproxy_for_account lần sau trả None
            # (chạy direct) thay vì đưa lại proxy chết cho task → task khỏi spin vô hạn.
            with self._kiotproxy_pool_lock:
                prev_cache = self._kiotproxy_pool_cache.get(k, {}) or {}
                if prev_cache.get("http"):
                    prev_cache["http"] = None
                    prev_cache["dead_marked_at"] = time.time()
                    self._kiotproxy_pool_cache[k] = prev_cache
            _filelog(
                f"[KiotProxy-Pool] ProxyError trên {acc_name} nhưng key[{key_idx}] "
                f"({k[:8]}...) đang cooldown {remain:.0f}s — đánh dấu cache DEAD, "
                f"trả None (account sẽ chạy direct đến khi cooldown hết)"
            )
            return None


        _filelog(
            f"[KiotProxy-Pool] ProxyError trên {acc_name} → rotate key[{key_idx}] "
            f"({k[:8]}...) để lấy IP mới"
        )
        data = self._fetch_kiotproxy_for_key(k, endpoint="new")
        if not data or not data.get("http"):
            return None
        http = data["http"]
        return http if http.startswith("http") else f"http://{http}"

    def _get_kiotproxy_for_account(self, acc) -> "str | None":
        """Map account → key trong pool → proxy URL đã cache.
        Sort active account (loại trừ excluded) theo name alphabet → index // M → key_idx.
        Account dư (index >= N*M) hoặc excluded → None (direct, log cảnh báo)."""
        keys = self._get_kiotproxy_pool_keys()
        if not keys:
            return None
        m = self._get_kiotproxy_accounts_per_key()
        acc_name = getattr(acc, "name", None)
        if not acc_name:
            return None
        excluded = self._get_kiotproxy_excluded_accounts()
        if acc_name in excluded:
            _filelog(
                f"[KiotProxy-Pool] Account {acc_name} đã bị user exclude "
                f"→ chạy direct (không proxy pool)"
            )
            return None
        try:
            accounts = [
                a for a in store.list_veo_accounts()
                if getattr(a, "is_active", False)
                and getattr(a, "name", "") not in excluded
            ]
        except Exception:
            return None
        accounts_sorted = sorted(accounts, key=lambda a: (getattr(a, "name", "") or "").lower())
        try:
            idx = next(i for i, a in enumerate(accounts_sorted) if getattr(a, "name", "") == acc_name)
        except StopIteration:
            return None
        key_idx = idx // m
        if key_idx >= len(keys):
            _filelog(
                f"[KiotProxy-Pool] Account {acc_name} (idx={idx}) vượt ngoài pool "
                f"(N={len(keys)} keys × M={m}) → chạy direct (no proxy)"
            )
            return None
        k = keys[key_idx]
        with self._kiotproxy_pool_lock:
            cache = dict(self._kiotproxy_pool_cache.get(k, {}))
        http = cache.get("http")
        if not http:
            # Cache chưa có — TÔN TRỌNG local rotate cooldown (KiotProxy giới hạn
            # 2 phút / key). Nếu đang cooldown, KHÔNG gọi /new (sẽ bị server rate-limit
            # lâu hơn). Trả None → account tạm chạy direct; background pool refresh
            # thread sẽ tự fetch khi cooldown hết.
            in_cd, remain = self._is_rotate_in_cooldown(k)
            if in_cd:
                _filelog(
                    f"[KiotProxy-Pool] Account {acc_name} key[{key_idx}] ({k[:6]}...) "
                    f"đang cooldown {remain:.0f}s → direct (no proxy) cho attempt này"
                )
                return None
            data = self._fetch_kiotproxy_for_key(k, endpoint="new")
            http = (data or {}).get("http") if data else None
            if not http:
                return None
        if not http.startswith("http"):
            http = f"http://{http}"
        return http

    def _get_global_proxy(self) -> "str | None":
        if getattr(self, "disable_media_proxy", True):
            return None
        """Đọc proxy từ KiotProxy (ưu tiên) hoặc Global Rotating Proxy từ settings DB.
        Trả về proxy URL đã normalize, hoặc None nếu chưa cấu hình.

        LƯU Ý: Dùng cho luồng không có `acc` (captcha, legacy). Với per-account
        mapping → gọi `_resolve_proxy_for_account(acc)`."""
        # 1. Thử pool (key đầu tiên có cache) — để code cũ không có acc vẫn dùng được
        try:
            keys = self._get_kiotproxy_pool_keys()
            if keys:
                with self._kiotproxy_pool_lock:
                    for k in keys:
                        cache = self._kiotproxy_pool_cache.get(k, {})
                        http = cache.get("http")
                        if http:
                            return http if http.startswith("http") else f"http://{http}"
        except Exception as e:
            _filelog(f"[KiotProxy-Pool] _get_global_proxy pool error: {e}")

        # 2. Legacy single-key fallback
        try:
            kiot_key = store.get_setting("kiotproxy_key", "")
            if kiot_key and kiot_key.strip():
                data = self.get_kiotproxy_current()
                if data and data.get("http"):
                    http_val = data["http"]
                    if not http_val.startswith("http"):
                        http_val = f"http://{http_val}"
                    return http_val
        except Exception as e:
            _filelog(f"[KiotProxy] _get_global_proxy error: {e}")

        # 3. Fallback → static global_proxy (legacy)
        try:
            raw = store.get_setting("global_proxy", "")
            if raw and raw.strip():
                return self._normalize_proxy_url(raw.strip())
        except Exception:
            pass
        return None

    def _resolve_proxy_for_account(self, acc) -> "str | None":
        if getattr(self, "disable_media_proxy", True):
            return None
        """Xác định proxy cho 1 account.
        Ưu tiên: account.proxy riêng → KiotProxy key riêng → proxy_pool.txt → None (direct).
        
        QUAN TRỌNG: KHÔNG dùng _get_global_proxy() ở đây vì nó lấy bừa key 
        của account khác trong pool cache → phá vỡ isolation per-account 
        (10 account cùng 1 IP → Google ban hàng loạt).
        """
        acc_name = getattr(acc, "name", "?")
        # 1. Account có proxy riêng?
        acc_proxy = getattr(acc, "proxy", None)
        if acc_proxy and str(acc_proxy).strip():
            normalized = self._normalize_proxy_url(str(acc_proxy).strip())
            if normalized:
                return normalized
        # 2. KiotProxy pool — CHỈ lấy key gắn riêng cho account này
        pooled = self._get_kiotproxy_for_account(acc)
        if pooled:
            return pooled
        # 3. Fallback → proxy_pool.txt (static pool, thoải mái dùng chung)
        try:
            from core.static_proxy_pool import get_random_proxy
            fallback = get_random_proxy()
            if fallback:
                _filelog(
                    f"[ProxyResolve] Account {acc_name}: KiotProxy key cooldown "
                    f"→ dùng static proxy: {fallback[:50]}"
                )
                return fallback
        except Exception as _sp_err:
            _filelog(f"[ProxyResolve] static_proxy_pool error: {_sp_err}")
        # 4. Không có proxy nào → chạy direct
        _filelog(f"[ProxyResolve] Account {acc_name}: không có proxy nào → direct")
        return None

    def _resolve_client_for_task(self, task: VideoTask, exclude_names: set = None):
        """
        Trả về VeoClient phù hợp cho task.
        Thứ tự ưu tiên:
          1. Cookie riêng của task (veo_cookie) — nếu lấy được token
          2. Lấy ngẫu nhiên từ Pool VeoAccount (thử tất cả accounts, skip exclude_names)
        Trả None nếu pool không còn account dùng được — KHÔNG fallback cookies.json.

        Proxy resolution (cho mỗi account):
          account.proxy riêng → Global Rotating Proxy (settings) → không proxy
        """
        from core.veo_client import VeoClient
        from core.hybrid_veo_client import HybridVeoClient

        exclude_names = exclude_names or set()

        def _try_client(cookie, proxy, label: str, account_email: str = None):
            """Tạo client và kiểm tra token. Trả về client nếu OK, None nếu thất bại.
            account_email != None → dùng HybridVeoClient (browser bridge khi Chrome
            connected, fallback httpx). Nếu None (task-cookie) → VeoClient httpx."""
            try:
                if account_email:
                    if _veo_browser_runtime_enabled():
                        # Unified Single-VPS runtime: không đi BrowserFlow/extension
                        # cho create video nữa. VeoClient sẽ tự execute request +
                        # reCAPTCHA trong Banana/VPS Chrome persistent lane.
                        client = VeoClient(cookie=cookie, proxy=proxy)
                        client._account_email = account_email
                    else:
                        client = HybridVeoClient(cookie=cookie, proxy=proxy, account_email=account_email)
                        # TĐ1 — Bridge ONLINE → skip httpx token validate.
                        # HybridVeoClient sau TĐ2 là lazy: chưa đụng VeoClient httpx →
                        # chưa gọi get_session_token() qua proxy. Khi Chrome bridge của
                        # account này đang connect, bridge tự lấy token trong tab khi
                        # call API → ta không cần access_token httpx ở đây. Tránh block
                        # 180s khi proxy chết.
                        try:
                            if client._browser is not None and client._browser.is_connected():
                                logger.info(
                                    f"[Client] {label}: bridge ONLINE → skip httpx validate, "
                                    f"return ngay (proxy={(proxy[:40] + '...') if proxy else 'none'})"
                                )
                                return client, True
                        except Exception:
                            pass
                        # Bridge offline → fall through validate httpx (sẽ trigger lazy init)
                else:
                    client = VeoClient(cookie=cookie, proxy=proxy)
                if not client.access_token:
                    logger.warning(
                        f"[Client] {label}: access_token rỗng sau init, thử lại get_session_token"
                    )
                    client.get_session_token()
                if client.access_token:
                    logger.info(f"[Client] {label}: access_token OK ✓")
                    return client, True  # proxy OK

                # KHÔNG fallback sang proxy=None (direct).
                # Việc fallback sẽ làm nhiều account cùng chia sẻ IP của VPS
                # → Google phát hiện và ban (UNUSUAL_ACTIVITY) hàng loạt.
                if proxy:
                    logger.warning(
                        f"[Client] {label}: proxy failed/dead → abort client init (no direct fallback to protect IP)"
                    )
                else:
                    logger.error(
                        f"[Client] {label}: KHÔNG lấy được access_token – bỏ qua account này"
                    )
                return None, False
            except Exception as e:
                logger.error(f"[Client] {label}: exception khi khởi tạo: {e}")
                return None, False

        # 0. Pinned account — mediaId account-bound nên tuyệt đối không swap.
        #    Cùng pattern với Section 1.5: cycle 3 proxy variants vô hạn, chỉ
        #    timeout 600s/1200s mới cancel. KHÔNG fall through dưới mọi hình thức.
        pinned = getattr(task, "pinned_account_name", None)
        if pinned:
            if pinned in exclude_names:
                logger.error(
                    f"[Client] Task {task.id}: pinned {pinned} bị exclude "
                    f"(upload fail / 401) → abort, KHÔNG swap"
                )
                return None
            if pinned in self._account_project_blacklist:
                logger.error(
                    f"[Client] Task {task.id}: pinned {pinned} bị project-blacklist → abort"
                )
                return None
            pinned_acc = store.get_veo_account_by_name(pinned)
            if not pinned_acc or not getattr(pinned_acc, "is_active", False):
                logger.error(
                    f"[Client] Task {task.id}: pinned {pinned} dead/inactive → abort"
                )
                return None

            _get_fresh_static = None  # media proxy disabled by default; static pool only used if re-enabled

            # ── TĐ3: Bridge fast-path ──
            # Nếu Chrome bridge của pinned account đang ONLINE → init client
            # tức thì với proxy default. `_try_client` (TĐ1) skip httpx validate
            # khi bridge online → return ngay. Tránh phải cycle 3 proxy variants
            # × 180s khi bridge có thể phục vụ luôn không cần proxy.
            try:
                from core.browser_task_server import is_account_connected as _is_conn
                if _is_conn(pinned_acc.name):
                    try:
                        _v1_fast = self._resolve_proxy_for_account(pinned_acc)
                    except Exception:
                        _v1_fast = None
                    _client_fast, _proxy_ok_fast = _try_client(
                        pinned_acc.cookie, _v1_fast,
                        f"Task {task.id} (pinned:{pinned_acc.name} bridge-fast-path)",
                        account_email=pinned_acc.name,
                    )
                    if _client_fast:
                        self._proxy_health[pinned_acc.name] = 'alive' if _proxy_ok_fast else 'unknown'
                        _gp = self._get_global_proxy()
                        _client_fast._rotating_proxy = None
                        _client_fast._account_label = pinned_acc.name
                        _filelog(
                            f"[Client] PINNED:{pinned_acc.name} bridge-fast-path OK "
                            f"(skip cycle proxy)"
                        )
                        return _client_fast
            except Exception:
                pass

            cycle = 0
            while True:
                cycle += 1
                # Timeout guard duy nhất (600s ảnh / 1200s video)
                if self._check_task_timeout(task):
                    logger.warning(
                        f"[Client] Task {task.id}: pinned {pinned_acc.name} TIMEOUT "
                        f"giữa cycle {cycle} retry proxy → return None"
                    )
                    return None

                # Build proxy variants — KHÔNG rotate KiotProxy (sẽ ban IP).
                # Dùng static_proxy_pool (200+ proxy) làm nguồn rotate chính.
                # Variant 1: proxy mặc định (account.proxy / KiotProxy / static fallback)
                # Variant 2-3: random fresh từ static pool, mark dead variant trước khi fail
                proxy_variants = []
                _used = set()
                try:
                    _v1 = self._resolve_proxy_for_account(pinned_acc)
                    proxy_variants.append(_v1)
                    if _v1: _used.add(_v1)
                except Exception:
                    proxy_variants.append(None)
                if not getattr(self, "disable_media_proxy", True):
                    for _i in range(2):
                        _next_proxy = None
                        try:
                            _next_proxy = _get_fresh_static(exclude=_used)
                        except Exception:
                            _next_proxy = None
                        proxy_variants.append(_next_proxy)
                        if _next_proxy: _used.add(_next_proxy)

                for proxy_idx, resolved_proxy in enumerate(proxy_variants):
                    client, _proxy_ok = _try_client(
                        pinned_acc.cookie,
                        resolved_proxy,
                        f"Task {task.id} (pinned:{pinned_acc.name} cycle{cycle}/proxy{proxy_idx+1}/{len(proxy_variants)})",
                        account_email=pinned_acc.name,
                    )
                    if client:
                        self._proxy_health[pinned_acc.name] = 'alive' if _proxy_ok else 'dead'
                        _gp = self._get_global_proxy()
                        client._rotating_proxy = None
                        client._account_label = pinned_acc.name
                        _filelog(
                            f"[Client] PINNED:{pinned_acc.name} cycle{cycle}/proxy{proxy_idx+1} OK "
                            f"proxy={'YES: '+resolved_proxy[:40] if resolved_proxy else 'DIRECT'}"
                        )
                        return client
                    if proxy_idx + 1 < len(proxy_variants):
                        time.sleep(0.5)

                # Mark các proxy đã thử là dead 60s để cycle sau pool tự loại
                for _bad in proxy_variants:
                    if _bad:
                        try:
                            from core.static_proxy_pool import mark_proxy_dead as _mark_dead
                            _mark_dead(_bad)
                        except Exception:
                            pass
                logger.warning(
                    f"[Client] Task {task.id}: pinned {pinned_acc.name} cycle {cycle} "
                    f"({len(proxy_variants)} proxy variants) đều fail → "
                    f"sleep 15s rồi cycle lại (chỉ timeout 600s/1200s mới cancel)"
                )
                time.sleep(15)

        # 1. Cookie riêng của task
        if task.veo_cookie and "task-cookie" not in exclude_names:
            logger.info(f"[Client] Task {task.id}: sử dụng cookie riêng của task")
            client, _proxy_ok = _try_client(
                task.veo_cookie, None, f"Task {task.id} (task-cookie)"
            )
            if client:
                client._account_label = "task-cookie"
                return client
            logger.warning(
                f"[Client] Task {task.id}: task-cookie thất bại → thử fallback sang Pool"
            )

        # 1.5. Ưu tiên account đã được cấp phát & đang bị giữ khoá bởi Smart Queue
        # FIX BUG "[Attempt 1] No available account": khi proxy mặc định chết
        # (DNS fail / 402), KHÔNG được fall through xuống Pool (sẽ swap account khác
        # = phá mediaId account-bound). Retry proxy VÔ HẠN trên CÙNG cookie locked,
        # chỉ thoát khi: (a) init thành công, hoặc (b) `_check_task_timeout` cancel
        # (600s ảnh / 1200s video). Tất cả proxy fail trong 1 cycle → sleep 5s rồi cycle lại.
        if task.picked_account_name and task.picked_account_name not in exclude_names:
            if task.picked_account_name in self._account_project_blacklist:
                logger.warning(
                    f"[Client] Task {task.id}: locked account {task.picked_account_name} is blacklisted (token expired) → skip"
                )
            else:
                acc = store.get_veo_account_by_name(task.picked_account_name)
                if acc and getattr(acc, "is_active", False):
                    _get_fresh_static = None  # media proxy disabled by default; static pool only used if re-enabled

                    # ── TĐ3: Bridge fast-path (locked picked account) ──
                    # Cùng cơ chế với pinned: bridge online → init client
                    # tức thì + skip cycle proxy.
                    try:
                        from core.browser_task_server import is_account_connected as _is_conn
                        if _is_conn(acc.name):
                            try:
                                _v1_fast = self._resolve_proxy_for_account(acc)
                            except Exception:
                                _v1_fast = None
                            _client_fast, _proxy_ok_fast = _try_client(
                                acc.cookie, _v1_fast,
                                f"Task {task.id} (locked_pool:{acc.name} bridge-fast-path)",
                                account_email=acc.name,
                            )
                            if _client_fast:
                                self._proxy_health[acc.name] = 'alive' if _proxy_ok_fast else 'unknown'
                                _gp = self._get_global_proxy()
                                _client_fast._rotating_proxy = None
                                _client_fast._account_label = acc.name
                                _filelog(
                                    f"[Client] locked_pool:{acc.name} bridge-fast-path OK "
                                    f"(skip cycle proxy)"
                                )
                                return _client_fast
                    except Exception:
                        pass

                    cycle = 0
                    while True:
                        cycle += 1
                        # Timeout guard duy nhất — nếu vượt 600s/1200s, _check_task_timeout
                        # tự set FAILED + release cookie, ta chỉ cần thoát.
                        if self._check_task_timeout(task):
                            logger.warning(
                                f"[Client] Task {task.id}: locked {acc.name} TIMEOUT "
                                f"giữa cycle {cycle} retry proxy → return None"
                            )
                            return None

                        # Build proxy variants — KHÔNG rotate KiotProxy (gây ban IP).
                        # Dùng static_proxy_pool (200+ proxy) làm nguồn rotate chính.
                        proxy_variants = []
                        _used = set()
                        try:
                            _v1 = self._resolve_proxy_for_account(acc)
                            proxy_variants.append(_v1)
                            if _v1: _used.add(_v1)
                        except Exception:
                            proxy_variants.append(None)
                        if not getattr(self, "disable_media_proxy", True):
                            for _i in range(2):
                                _next_proxy = None
                                try:
                                    _next_proxy = _get_fresh_static(exclude=_used)
                                except Exception:
                                    _next_proxy = None
                                proxy_variants.append(_next_proxy)
                                if _next_proxy: _used.add(_next_proxy)

                        for proxy_idx, resolved_proxy in enumerate(proxy_variants):
                            client, _proxy_ok = _try_client(
                                acc.cookie,
                                resolved_proxy,
                                f"Task {task.id} (locked_pool:{acc.name} cycle{cycle}/proxy{proxy_idx+1}/{len(proxy_variants)})",
                                account_email=acc.name,
                            )
                            if client:
                                self._proxy_health[acc.name] = 'alive' if _proxy_ok else 'dead'
                                _gp = self._get_global_proxy()
                                client._rotating_proxy = None
                                client._account_label = acc.name
                                _filelog(
                                    f"[Client] locked_pool:{acc.name} cycle{cycle}/proxy{proxy_idx+1} OK "
                                    f"proxy={'YES: '+resolved_proxy[:40] if resolved_proxy else 'DIRECT'}"
                                )
                                return client
                            if proxy_idx + 1 < len(proxy_variants):
                                time.sleep(0.5)

                        # Mark proxy đã thử là dead 60s để cycle sau pool tự loại
                        for _bad in proxy_variants:
                            if _bad:
                                try:
                                    from core.static_proxy_pool import mark_proxy_dead as _mark_dead
                                    _mark_dead(_bad)
                                except Exception:
                                    pass
                        # Cả cycle fail → log và sleep, KHÔNG fall through Pool (tránh swap).
                        logger.warning(
                            f"[Client] Task {task.id}: locked {acc.name} cycle {cycle} "
                            f"({len(proxy_variants)} proxy variants) đều fail → "
                            f"sleep 15s rồi cycle lại (chỉ timeout 600s/1200s mới cancel)"
                        )
                        time.sleep(15)

        # 2. Thử lấy từ Pool (tất cả accounts, skip exclude_names)
        #
        # BUG FIX: Trước đây section này set `picked_account_name = acc.name` nhưng
        # KHÔNG tăng `_cookie_busy_veo[acc.name]` → khi task swap account, account
        # mới được pick không reserve slot → task khác trong queue vẫn claim account
        # đó bình thường → 2 task chạy song song trên cùng account (phá vỡ
        # _MAX_VIDEO_PER_ACCOUNT=1). Fix: atomic check-and-reserve slot.
        all_accounts = store.list_veo_accounts()
        import random as _rand

        _rand.shuffle(all_accounts)  # random thứ tự
        tried_ids = set()

        # Xác định action type để dùng đúng limit
        from web.models import ActionType as _AT
        _action = getattr(task, "action_type", None)
        _is_video = _action in {
            _AT.TEXT_TO_VIDEO, _AT.IMAGE_TO_VIDEO,
            _AT.IMAGES_TO_VIDEO, _AT.FRAMES_TO_VIDEO,
        }
        _max_per_acc = (
            self._MAX_VIDEO_PER_ACCOUNT if _is_video else self._MAX_IMAGE_PER_ACCOUNT
        )

        # ── ƯU TIÊN account có Chrome bridge connected ──
        # Sort: bridge accounts trước, non-bridge sau (chỉ chạm tới khi bridge hết slot)
        try:
            from core.browser_task_server import is_account_connected as _is_conn
            _bridge_set = {a.name for a in all_accounts if getattr(a, "is_active", False) and _is_conn(a.name)}
            if _bridge_set:
                # Stable sort: account có bridge lên đầu
                all_accounts = sorted(all_accounts, key=lambda a: (0 if a.name in _bridge_set else 1))
        except Exception:
            pass

        for acc in all_accounts:
            if not getattr(acc, "is_active", False):
                continue
            if acc.name in exclude_names:
                continue
            if acc.name in self._account_project_blacklist:
                continue
            if acc.id in tried_ids:
                continue
            tried_ids.add(acc.id)

            # ── Atomic check-and-reserve: skip nếu account đã đạt limit busy ──
            _reserved = False
            with self._cookie_cond:
                _cur_busy = self._cookie_busy_veo.get(acc.name, 0)
                if _cur_busy < _max_per_acc:
                    self._cookie_busy_veo[acc.name] = _cur_busy + 1
                    _reserved = True
            if not _reserved:
                _filelog(
                    f"[Client] Pool account={acc.name} đang busy ({_cur_busy}/{_max_per_acc}) "
                    f"→ skip (tránh chạy song song trên cùng account)"
                )
                continue

            # Xác định proxy: account riêng → global → None
            resolved_proxy = self._resolve_proxy_for_account(acc)
            _filelog(
                f"[Client] Pool account={acc.name} proxy={'YES: '+resolved_proxy[:40] if resolved_proxy else 'DIRECT (no proxy)'} "
                f"(reserved slot {_cur_busy + 1}/{_max_per_acc})"
            )
            self._set_activity(acc.name, "RESOLVING_COOKIE", getattr(task, "id", ""))
            try:
                client, _proxy_ok = _try_client(acc.cookie, resolved_proxy, f"Task {task.id} (pool:{acc.name})", account_email=acc.name)
                if client:
                    self._proxy_health[acc.name] = 'alive' if _proxy_ok else 'dead'
                    # Lưu global proxy gốc để rotate session ID (nếu proxy hỗ trợ)
                    _gp = self._get_global_proxy()
                    client._rotating_proxy = None
                    client._account_label = acc.name
                    # Track lại account nào được chọn để Admin có thể xem
                    task.picked_account_name = acc.name
                    store.update_video_task(task)
                    # ✓ Slot đã reserve → caller chịu trách nhiệm _release_cookie
                    #   khi task done (worker finally) hoặc swap sang account khác.
                    return client
                else:
                    if resolved_proxy:
                        self._proxy_health[acc.name] = 'dead'
                    self._clear_activity(acc.name)
                    # ⚠ Client init fail → release slot đã reserve để account khác dùng
                    with self._cookie_cond:
                        self._cookie_busy_veo[acc.name] = max(
                            0, self._cookie_busy_veo.get(acc.name, 0) - 1
                        )
                        self._cookie_cond.notify_all()
            except Exception as _pool_err:
                logger.warning(f"[Client] Pool {acc.name}: exception in _try_client: {_pool_err}")
                self._clear_activity(acc.name)
                # ⚠ Exception → release slot đã reserve
                with self._cookie_cond:
                    self._cookie_busy_veo[acc.name] = max(
                        0, self._cookie_busy_veo.get(acc.name, 0) - 1
                    )
                    self._cookie_cond.notify_all()

        if tried_ids:
            logger.error(
                f"[Client] Task {task.id}: Tất cả {len(tried_ids)} pool accounts đều thất bại"
            )

        # KHÔNG fallback cookies.json — caller phải xử lý None (đợi pool, retry, hoặc FAIL task).
        # Lý do: cookies.json là global single-cookie, dùng chung thì phá vỡ invariant
        # 1-task-1-account, đếm sai slot, và không có proxy per-account.
        logger.error(
            f"[Client] Task {task.id}: Không còn pool account nào dùng được → trả None"
        )
        return None

    @staticmethod
    def _get_rotating_proxy(client) -> "str | None":
        """Lấy rotating proxy từ client để dùng cho captcha.
        Ưu tiên _rotating_proxy (set bởi _resolve_client_for_task),
        fallback về client.proxy thông thường."""
        return getattr(client, "_rotating_proxy", None) or getattr(
            client, "proxy", None
        )

    # (403 Auto-Ban đã xóa — 403 = lỗi captcha worker, không phải account tạo)

    # ─── Captcha ───

    def _solve_captcha_smart(
        self,
        task_client,
        action: str = "VIDEO_GENERATION",
    ) -> str:
        """
        Lấy captcha token từ Kho Token Chung (CaptchaPool).

        Kho được fill liên tục bởi 2 background producer:
          • Extension — giải trên labs.google thật
          • Remote Server — giải trên server ngoài

        Token nào có trong kho → lấy ngay, không chờ giải.
        """
        if action == "VIDEO_GENERATION" and _veo_browser_runtime_enabled():
            account_label = getattr(task_client, "_account_label", None)
            self._set_activity(account_label or "", "BROWSER_RUNTIME_CAPTCHA", "")
            if hasattr(task_client, "__dict__"):
                task_client._last_pool_entry = None
            logger.info(
                "[Captcha-Smart] Browser runtime enabled → skip legacy "
                "VIDEO_GENERATION CaptchaPool/RemoteCaptcha"
            )
            return "BROWSER_RUNTIME_RECAPTCHA_IN_PAGE"

        from core.captcha_pool import get_pool

        account_label = getattr(task_client, "_account_label", None)
        self._set_activity(account_label or "", "SOLVING_CAPTCHA", "")

        pool = get_pool()
        # Đảm bảo pool đang chạy với đúng action
        if not pool.running:
            pool.start(action=action)
        else:
            pool.set_action(action)

        logger.info(
            f"[Captcha-Smart] Lấy token từ kho (action={action}, "
            f"kho hiện có={pool.size})..."
        )

        entry = pool.get_token(timeout=30, action=action)
        if entry:
            logger.info(
                f"[Captcha-Smart] ✅ Got token: {entry.source} "
                f"(len={len(entry.token)}, use #{entry.use_count})"
            )
            # Ghi nhận thống kê: nhận token từ nguồn nào
            self._record_captcha_stat(account_label or "", entry.source, "received")
            # Lưu entry vào task_client để report_result sau khi gọi API
            if hasattr(task_client, "__dict__"):
                task_client._last_pool_entry = entry
            return entry.token

        self._clear_activity(account_label or "")
        logger.warning("[Captcha-Smart] ⏰ Kho trống, timeout → trả empty token")
        return ""

    def _deferred_upscale_loop(self):
        """Background thread: poll kết quả upscale 1080p mỗi 60s.
        Nếu 1080p sẵn sàng → tải về ghi đè file 720p.
        Nếu quá 20 phút → giữ nguyên 720p.
        """
        _filelog("[Upscale-BG] Background upscale polling thread started")
        while True:
            try:
                time.sleep(10)  # Check queue mỗi 10s

                now = time.time()
                items_to_process = []

                with self._deferred_upscale_lock:
                    for item in self._deferred_upscale_queue:
                        if now >= item["next_check_at"]:
                            items_to_process.append(item)

                if not items_to_process:
                    continue

                for item in items_to_process:
                    task_id = item["task_id"]
                    op_name = item["op_name"]
                    scene_id = item["scene_id"]
                    _client = item["client"]
                    elapsed = now - item["submitted_at"]

                    try:
                        # Poll 1 lần
                        poll_resp = _client.check_status_batch(
                            0, op_name, scene_id, "MEDIA_GENERATION_STATUS_PENDING"
                        )
                    except Exception as e:
                        logger.warning(f"[Upscale-BG] Task {task_id}: poll error: {e}")
                        poll_resp = None

                    if poll_resp:
                        st = _extract_status(poll_resp)
                        video_url = _extract_video_url(poll_resp)

                        if ("COMPLETE" in st or "SUCCESS" in st or "DONE" in st) and video_url:
                            # ✅ 1080p sẵn sàng! Tải về ghi đè file 720p
                            logger.info(
                                f"[Upscale-BG] Task {task_id}: 1080p READY! "
                                f"Downloading to overwrite 720p (elapsed={int(elapsed)}s)"
                            )
                            _filelog(
                                f"[Upscale-BG] Task {task_id}: 1080p URL={video_url[:60]}..."
                            )
                            try:
                                from core import browser_config as bcfg
                                headers = {"User-Agent": bcfg.get("user_agent", "Mozilla/5.0")}
                                _download_video(
                                    video_url,
                                    item["file_path"],
                                    headers=headers,
                                    aspect=item.get("screen_ratio", "16:9"),
                                )
                                logger.info(
                                    f"[Upscale-BG] Task {task_id}: ✅ 1080p saved! "
                                    f"Overwritten {item['file_path']}"
                                )
                                _filelog(
                                    f"[Upscale-BG] Task {task_id}: 1080p DONE → {item['file_path']}"
                                )
                            except Exception as e:
                                logger.warning(
                                    f"[Upscale-BG] Task {task_id}: download 1080p failed: {e}. "
                                    f"Giữ nguyên 720p."
                                )
                            # Xóa khỏi queue
                            with self._deferred_upscale_lock:
                                if item in self._deferred_upscale_queue:
                                    self._deferred_upscale_queue.remove(item)
                            continue

                        elif "FAIL" in st or "ERROR" in st or "CANCEL" in st:
                            # ❌ Google báo lỗi → giữ 720p
                            logger.warning(
                                f"[Upscale-BG] Task {task_id}: upscale FAILED (status={st}). Giữ 720p."
                            )
                            with self._deferred_upscale_lock:
                                if item in self._deferred_upscale_queue:
                                    self._deferred_upscale_queue.remove(item)
                            continue

                    # Chưa xong — kiểm tra timeout
                    if elapsed > DEFERRED_UPSCALE_MAX_WAIT:
                        logger.warning(
                            f"[Upscale-BG] Task {task_id}: TIMEOUT {int(elapsed)}s > "
                            f"{DEFERRED_UPSCALE_MAX_WAIT}s. Giữ nguyên 720p."
                        )
                        _filelog(
                            f"[Upscale-BG] Task {task_id}: TIMEOUT → giữ 720p"
                        )
                        with self._deferred_upscale_lock:
                            if item in self._deferred_upscale_queue:
                                self._deferred_upscale_queue.remove(item)
                        continue

                    # Chưa xong, chưa timeout → schedule check tiếp sau 60s
                    item["next_check_at"] = now + DEFERRED_UPSCALE_POLL_INTERVAL
                    logger.info(
                        f"[Upscale-BG] Task {task_id}: chưa xong (elapsed={int(elapsed)}s). "
                        f"Check lại sau {DEFERRED_UPSCALE_POLL_INTERVAL}s."
                    )

            except Exception as e:
                logger.error(f"[Upscale-BG] Loop error: {e}")
                time.sleep(30)

    def _poll_until_completed(
        self, task: VideoTask, client, op_name: str, scene_id: str
    ) -> tuple[Optional[str], Optional[str]]:
        """
        Poll Google Labs cho đến khi video sẵn sàng.
        Trả về (video_url, media_id) (không download về VPS).
        """
        _client = client or self.client

        if not op_name:
            logger.error(f"[Poll] Task {task.id}: op_name rỗng, không thể poll")
            task.error = "op_name rỗng — submit thất bại."
            return None, None

        video_url = None
        media_id = None
        elapsed = 0
        current_status = "MEDIA_GENERATION_STATUS_PENDING"
        prev_st = None

        logger.info(f"[Poll] Task {task.id}: bắt đầu poll op={op_name[:60]}")
        _filelog(f"[Poll] Task {task.id}: op={op_name[:60]}")
        _poll_acc = getattr(task, "picked_account_name", None) or ""
        self._set_activity(_poll_acc, "POLLING_RESULT", task.id)

        # Track phản hồi hữu ích gần nhất — nếu im lặng quá POLL_SILENT_TIMEOUT giây
        # (liên tục exception hoặc response rỗng) → huỷ task để worker nhận task khác.
        last_useful_poll_at = time.time()

        while elapsed < POLL_MAX_WAIT:
            # ── Timeout check ──
            if self._check_task_timeout(task):
                return None, None
            
            # Initial delay vs Interval delay
            if elapsed == 0:
                time.sleep(POLL_INITIAL_DELAY)
                elapsed += POLL_INITIAL_DELAY
            else:
                time.sleep(POLL_INTERVAL)
                elapsed += POLL_INTERVAL

            try:
                poll_resp = _client.check_status_batch(
                    0, op_name, scene_id, current_status
                )
            except Exception as e:
                _err_str = str(e)
                # ── Proxy chết (10061) → xoay proxy ngay cho poll ──
                if "10061" in _err_str or "ProxyError" in _err_str:
                    if _poll_acc:
                        new_ip = self._record_ip_error(_poll_acc)
                        if new_ip:
                            _client.proxy = new_ip
                            logger.info(
                                f"[Poll] Task {task.id}: proxy chết → xoay IP mới: {new_ip}"
                            )
                            last_useful_poll_at = time.time()  # Reset silent timer
                            continue  # Retry poll ngay với proxy mới
                
                logger.warning(f"[Poll] Task {task.id}: check_status_batch error: {e}")
                if time.time() - last_useful_poll_at > POLL_SILENT_TIMEOUT:
                    # ── Fallback: thử tải 720p trực tiếp ──
                    logger.warning(
                        f"[Poll] Task {task.id}: SILENT TIMEOUT {POLL_SILENT_TIMEOUT}s (exception) → "
                        f"thử tải 720p qua MediaRedirect"
                    )
                    _fallback_url = None
                    if op_name:
                        try:
                            _fallback_url = _client.get_media_download_url(op_name)
                        except Exception:
                            pass
                        # Retry không proxy nếu proxy fail
                        if not _fallback_url:
                            try:
                                logger.info(f"[Poll] Task {task.id}: MediaRedirect proxy fail → thử DIRECT (no proxy)")
                                _fallback_url = _client.get_media_download_url(op_name, no_proxy=True)
                            except Exception:
                                pass
                    if _fallback_url:
                        logger.info(f"[Poll] Task {task.id}: ✅ Fallback thành công!")
                        return _fallback_url, scene_id

                    task.error = (
                        f"Poll không nhận được phản hồi trong {POLL_SILENT_TIMEOUT}s "
                        f"(liên tục exception). Fallback tải 720p cũng thất bại."
                    )
                    logger.error(
                        f"[Poll] Task {task.id}: SILENT TIMEOUT {POLL_SILENT_TIMEOUT}s (exception + fallback failed)"
                    )
                    return None, None
                continue

            if not poll_resp:
                logger.warning(
                    f"[Poll] Task {task.id}: poll_resp rỗng (elapsed={elapsed}s)"
                )
                # ── Thử xoay proxy nếu response liên tục rỗng (có thể proxy chết) ──
                if _poll_acc and (time.time() - last_useful_poll_at > 30):
                    new_ip = self._record_ip_error(_poll_acc)
                    if new_ip:
                        _client.proxy = new_ip
                        logger.info(
                            f"[Poll] Task {task.id}: response rỗng >30s → xoay proxy: {new_ip}"
                        )
                        last_useful_poll_at = time.time()
                        continue
                
                if time.time() - last_useful_poll_at > POLL_SILENT_TIMEOUT:
                    # ── Fallback: thử tải 720p trực tiếp qua getMediaUrlRedirect ──
                    # Xoay proxy 1 lần nữa trước khi fallback (để không dùng proxy chết)
                    if _poll_acc:
                        _fb_ip = self._record_ip_error(_poll_acc)
                        if _fb_ip:
                            _client.proxy = _fb_ip
                    
                    logger.warning(
                        f"[Poll] Task {task.id}: SILENT TIMEOUT {POLL_SILENT_TIMEOUT}s → "
                        f"thử tải 720p trực tiếp qua MediaRedirect (op_name={op_name[:20] if op_name else '?'})"
                    )
                    _fallback_url = None
                    if op_name:
                        try:
                            _fallback_url = _client.get_media_download_url(op_name)
                        except Exception as _fb_err:
                            logger.warning(f"[Poll] MediaRedirect fallback error: {_fb_err}")
                        # Retry không proxy nếu proxy fail
                        if not _fallback_url:
                            try:
                                logger.info(f"[Poll] Task {task.id}: MediaRedirect proxy fail → thử DIRECT (no proxy)")
                                _fallback_url = _client.get_media_download_url(op_name, no_proxy=True)
                            except Exception as _fb_err2:
                                logger.warning(f"[Poll] MediaRedirect no-proxy fallback error: {_fb_err2}")

                    if _fallback_url:
                        logger.info(
                            f"[Poll] Task {task.id}: ✅ Fallback thành công! "
                            f"Got 720p URL qua MediaRedirect"
                        )
                        return _fallback_url, scene_id

                    # Fallback thất bại → huỷ task
                    task.error = (
                        f"Poll trả response rỗng liên tục trong {POLL_SILENT_TIMEOUT}s. "
                        f"Fallback tải 720p trực tiếp cũng thất bại. Huỷ task."
                    )
                    logger.error(
                        f"[Poll] Task {task.id}: SILENT TIMEOUT {POLL_SILENT_TIMEOUT}s "
                        f"(empty resp + MediaRedirect fallback failed)"
                    )
                    return None, None
                continue

            # Nhận được response hữu ích → reset silent timer
            last_useful_poll_at = time.time()

            st = _extract_status(poll_resp)
            current_status = st

            if st != prev_st:
                logger.info(f"[Poll] Task {task.id}: elapsed={elapsed}s status={st}")
                _filelog(f"[Poll] Task {task.id}: elapsed={elapsed}s status={st}")
                prev_st = st

            if "ACTIVE" in st or "PENDING" in st or st == "UNKNOWN":
                continue

            video_url = _extract_video_url(poll_resp)
            media_id = _extract_media_id(poll_resp)

            if "COMPLETE" in st or "SUCCESS" in st or "DONE" in st:
                logger.info(
                    f"[Poll] Task {task.id}: COMPLETE url={'found' if video_url else 'NOT FOUND'}"
                )
                break
            elif "FAIL" in st or "ERROR" in st or "CANCEL" in st:
                logger.error(f"[Poll] Task {task.id}: terminal status={st}")
                logger.error(f"[Poll] Google Backend Error Reason JSON: {poll_resp}")

                # Trích xuất mã lỗi cụ thể từ poll_resp
                _err_code = ""
                _err_msg = ""
                try:
                    ops = poll_resp.get("operations", []) if isinstance(poll_resp, dict) else []
                    for op_item in ops:
                        op_err = op_item.get("operation", {}).get("error", {})
                        if op_err:
                            _err_code = op_err.get("code", "")
                            _err_msg = op_err.get("message", "")
                            break
                except Exception:
                    pass

                # ── Ghi nhận IP error nếu Google reject vì proxy IP ──
                _err_combined = str(_err_msg) + str(_err_code)
                if any(x in _err_combined for x in ("IP_FILTER", "IP_INPUT", "PUBLIC_ERROR_IP")):
                    self._record_ip_error(_poll_acc)

                _err_suffix = ""
                if _err_code or _err_msg:
                    _err_suffix = f" [Mã lỗi: {_err_code} — {_err_msg}]"

                task.error = (
                    f"Video bị từ chối do vi phạm chính sách an toàn của Google "
                    f"(có thể do từ khóa nhạy cảm hoặc âm thanh có bản quyền/mã code lỗi Google).{_err_suffix}"
                )
                break
            else:
                if video_url:
                    logger.info(
                        f"[Poll] Task {task.id}: unknown status={st} but URL found"
                    )
                    break

        if not video_url:
            # ── Last resort: thử tải 720p trực tiếp qua MediaRedirect ──
            if op_name:
                logger.warning(
                    f"[Poll] Task {task.id}: Không có video_url từ poll → "
                    f"thử MediaRedirect fallback (op_name={op_name[:20]})"
                )
                try:
                    _fallback_url = _client.get_media_download_url(op_name)
                    if _fallback_url:
                        logger.info(f"[Poll] Task {task.id}: ✅ MediaRedirect fallback thành công!")
                        return _fallback_url, scene_id
                except Exception as _fb_err:
                    logger.warning(f"[Poll] MediaRedirect fallback error: {_fb_err}")

            if elapsed >= POLL_MAX_WAIT:
                task.error = f"Poll timeout sau {POLL_MAX_WAIT}s (fallback 720p cũng thất bại)"
                logger.error(f"[Poll] Task {task.id}: TIMEOUT sau {POLL_MAX_WAIT}s")
            return None, None

        logger.info(f"[Poll] Task {task.id}: URL={video_url[:80]}...")
        _filelog(f"[Poll] Task {task.id}: URL stored → {video_url[:80]}")
        return video_url, media_id

    def _poll_and_download(
        self,
        task: VideoTask,
        op_name: str,
        scene_id: str,
        prompt: str,
        client=None,
        captcha: str = "",
    ) -> Optional[str]:

        # 1. Poll lấy bản 720p
        video_url, media_id = self._poll_until_completed(
            task, client, op_name, scene_id
        )
        if not video_url:
            return None

        # 2. Tải về video 720p NGAY LẬP TỨC (không chờ upscale)
        try:
            output_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "outputs"
            )
            os.makedirs(output_dir, exist_ok=True)
            ext = ".mp4"
            filename = f"{task.id}{ext}"
            file_path = os.path.join(output_dir, filename)

            logger.info(
                f"[Download] Downloading 720p video for task {task.id} → {file_path}"
            )
            _filelog(f"[Download] URL={video_url[:80]} → {filename}")

            from core import browser_config as bcfg

            headers = {"User-Agent": bcfg.get("user_agent", "Mozilla/5.0")}
            _dl_acc = getattr(task, "picked_account_name", None) or ""
            self._set_activity(_dl_acc, "DOWNLOADING", task.id)
            _download_video(
                video_url, file_path, headers=headers, aspect=task.screen_ratio
            )

            logger.info(f"[Download] Saved 720p: {file_path}")
        except Exception as e:
            logger.error(f"[Download] Task {task.id}: download failed: {e}. Fallback to HTTP URL.")
            task.error = f"Lỗi tải video xuống server: {e}"
            # TRẢ VỀ URL THAY VÌ NONE. Như thế task vẫn sẽ COMPLETED.
            return video_url

        # 3. Gửi yêu cầu Upscale 1080p rồi GIẢI PHÓNG WORKER ngay
        #    Việc poll 1080p sẽ do background thread xử lý, không block worker.
        if media_id:
            _client = client or self.client
            try:
                logger.info(f"[Upscale-Deferred] Submitting 1080p upscale for mediaId={media_id}")
                upscale_res = _client.upscale_video(
                    row=0,
                    input_media_generation_id=media_id,
                    project_id=task.project_id,
                    aspect=task.screen_ratio,
                    captcha_token=captcha,
                )
                if upscale_res and upscale_res.get("op_name"):
                    logger.info(
                        f"[Upscale-Deferred] Upscale submitted OK. op={upscale_res['op_name'][:30]}. "
                        f"Worker giải phóng, background thread sẽ poll 1080p."
                    )
                    _filelog(
                        f"[Upscale-Deferred] Task {task.id}: op={upscale_res['op_name'][:30]} → deferred queue"
                    )
                    # Đẩy vào hàng đợi ngầm để background thread poll
                    with self._deferred_upscale_lock:
                        self._deferred_upscale_queue.append({
                            "task_id": task.id,
                            "op_name": upscale_res["op_name"],
                            "scene_id": upscale_res["scene_id"],
                            "video_url_720": video_url,
                            "file_path": file_path,
                            "client": _client,
                            "submitted_at": time.time(),
                            "next_check_at": time.time() + DEFERRED_UPSCALE_POLL_INTERVAL,
                            "account_name": getattr(task, "picked_account_name", None) or "",
                            "screen_ratio": task.screen_ratio,
                        })
                else:
                    logger.warning(
                        f"[Upscale-Deferred] upscale_video API call failed, giữ nguyên 720p"
                    )
            except Exception as e:
                logger.warning(f"[Upscale-Deferred] Exception khi submit upscale: {e}. Giữ 720p.")

        task.error = None
        return file_path

    # ─── Text-to-Video ───

    def create_text_to_video(
        self,
        project_id: str,
        name: str,
        model: str,
        screen_ratio: str,
        prompts: List[str],
        background: bool = True,
    ) -> VideoTask:
        task = VideoTask(
            id=str(uuid.uuid4()),
            project_id=project_id,
            name=name,
            action_type=ActionType.TEXT_TO_VIDEO,
            model=model,
            screen_ratio=screen_ratio,
            prompts=prompts,
            status=TaskStatus.PENDING,
        )
        store.create_video_task(task)

        if background:
            self.executor.submit(self._run_t2v, task)
        else:
            self._run_t2v(task)

        return task

    def _run_create_image(self, task: VideoTask):
        return self._run_banana_task(task)

    def _run_t2v(self, task: VideoTask):
        return self._run_banana_task(task)

    def _run_i2v(self, task: VideoTask):
        return self._run_banana_task(task)

    def _run_i2v_b64(self, task: VideoTask):
        return self._run_banana_task(task)

    def _old_run_t2v_unused(self, task: VideoTask):
        try:  # OUTER guard — bắt mọi crash kể cả OSError từ console encoding
            # ── Timeout check ──
            if self._check_task_timeout(task):
                return
            _filelog(f"[T2V] START task={task.id} model={task.model}")
            task.status = TaskStatus.PROCESSING
            store.update_video_task(task)

            prompt = task.prompts[0] if task.prompts else ""
            model_key = resolve_model_key(task.model, task.screen_ratio)

            # NO-SWAP: T2V retry vô hạn cùng account. Lỗi F5-recoverable
            # (UNUSUAL_ACTIVITY 403, 429 too much, RESOURCE_EXHAUSTED, 5xx) →
            # F5 tab labs.google + retry. Lỗi khác → sleep + retry. Chỉ timeout
            # 1200s cancel.
            T2V_MAX_ATTEMPTS = 999999
            last_error = None

            failed_accounts = set()  # giữ var cho tương thích, KHÔNG add
            account_locked = None    # email account khoá cứng cho task này
            for attempt in range(1, T2V_MAX_ATTEMPTS + 1):
                # ── Timeout guard duy nhất ──
                if self._check_task_timeout(task):
                    return
                # ── Check: task có bị PAUSED giữa chừng không? ──
                _fresh_t = store.get_video_task(task.id)
                if _fresh_t and str(getattr(_fresh_t, 'status', '')).upper() == 'PAUSED':
                    logger.info(f"[T2V] ⏸️ Task {task.id} đã bị PAUSED → dừng retry")
                    return

                _filelog(f"[T2V] Task {task.id}: attempt {attempt} (no-swap, locked={account_locked})")
                logger.info(
                    f"[T2V] Task {task.id}: attempt {attempt} (no-swap)"
                )

                # --- DYNAMIC CLIENT INITIALIZATION (lock account ở lần đầu) ---
                if account_locked and not task.picked_account_name:
                    task.picked_account_name = account_locked
                task_client = self._resolve_client_for_task(
                    task, exclude_names=set()  # KHÔNG exclude → giữ cùng account
                )
                if task_client is None:
                    # Account locked tạm thời chưa available — sleep + retry,
                    # KHÔNG break (timeout 1200s là guard duy nhất).
                    logger.warning(
                        f"[T2V] Task {task.id}: _resolve_client trả None "
                        f"(locked={account_locked}) → sleep 5s + retry"
                    )
                    time.sleep(5)
                    continue
                # Lock account ở lần pick đầu tiên
                if not account_locked:
                    account_locked = task.picked_account_name or getattr(task_client, "_account_label", None)
                    if account_locked:
                        task.picked_account_name = account_locked
                        _filelog(
                            f"[T2V] Task {task.id}: LOCKED account={account_locked} (no-swap mode)"
                        )
                        threading.Thread(
                            target=task_client.warmup_browser_runtime,
                            name=f"veo-warmup-{task.id[:8]}",
                            daemon=True,
                        ).start()

                # Bước 1: Tạo Google Labs project TRƯỚC
                task_client._account_label = task.picked_account_name
                gl_project = task.project_id
                try:
                    from core.project import create_project, search_user_projects

                    # Ưu tiên lấy project mới nhất đã có
                    proj = None
                    try:
                        _existing = search_user_projects(
                            cookie=task_client.cookie,
                            access_token=task_client.access_token,
                            page_size=1,
                            timeout=8,
                            proxy=task_client.proxy,
                        )
                        if _existing and len(_existing) > 0:
                            proj = _existing[0].get("projectId")
                            if proj:
                                logger.info(
                                    f"[T2V] Reusing existing GL project: {proj[:20]}..."
                                )
                    except Exception as _se:
                        logger.warning(f"[T2V] search_user_projects failed: {_se}")

                    # Fallback: tạo mới
                    if not proj:
                        proj = create_project(
                            f"API-{task.project_id[:8]}",
                            tool_name="PINHOLE",
                            cookie=task_client.cookie,
                            access_token=task_client.access_token,
                            browser_headers=getattr(task_client, "base_headers", None),
                            proxy=task_client.proxy,
                        )
                    if proj:
                        gl_project = proj
                        logger.info(
                            f"[Project] GL project ready: {gl_project[:20]}..."
                        )
                except Exception as e:
                    logger.warning(
                        f"[Project] Could not get/create Google Labs project: {e}"
                    )

                # Set project ID trên client để Referer chính xác
                task_client._current_project_id = gl_project

                # Bước 2: Giải captcha SAU project creation, ngay TRƯỚC API call
                # Token tươi nhất có thể → Google không reject 403
                captcha = self._solve_captcha_smart(
                    task_client,
                    "VIDEO_GENERATION",
                )
                if not captcha:
                    # NO-SWAP: captcha empty → retry SAME account (captcha pool
                    # tạm cạn). Sleep ngắn để pool refill.
                    logger.warning(
                        f"[T2V] Attempt {attempt}: captcha empty → sleep 2s + retry SAME account (no-swap)"
                    )
                    last_error = f"[Attempt {attempt}] Captcha failed (empty token)."
                    time.sleep(2)
                    continue

                logger.info(
                    f"[T2V] Task {task.id}: model={model_key} ratio={task.screen_ratio} captcha_len={len(captcha)}"
                )
                # Main API flow: do not serialize create per account.
                # Worker/thread count and token distribution control parallelism.
                self._set_activity(task.picked_account_name or "", "CALLING_API", task.id)
                result = task_client.create_video_t2v(
                    row=0,
                    prompt=prompt,
                    project_id=gl_project,
                    captcha_token=captcha,
                    aspect=task.screen_ratio,
                    model_key=model_key,
                )

                if not result:
                    # Lấy chi tiết lỗi thực sự từ Google API (status code, message)
                    api_detail = (
                        getattr(task_client, "_last_error_detail", None) or "no detail"
                    )
                    api_detail_raw = api_detail  # giữ bản gốc trước khi dịch
                    _is_403 = "403" in api_detail_raw

                    # ── Banana §06: Centralized error handling ──
                    _err_rv = self._handle_api_error_banana(
                        api_detail=api_detail_raw,
                        account_label=task.picked_account_name or "",
                        flow="T2V",
                        task_client=task_client,
                        captcha_action="VIDEO_GENERATION",
                    )

                    if _err_rv["should_fail_task"]:
                        task.status = TaskStatus.FAILED
                        task.error = _err_rv["fail_error"]
                        store.update_video_task(task)
                        return

                    if _err_rv["new_task_client"]:
                        task_client = _err_rv["new_task_client"]
                    if _err_rv["new_captcha"]:
                        captcha = _err_rv["new_captcha"]

                    last_error = f"Lỗi gửi yêu cầu tạo video (Lần {attempt}). {api_detail}"

                    if attempt < T2V_MAX_ATTEMPTS:
                        _is_captcha_action = _err_rv["action"] in (ErrorAction.F5_RETRY,)
                        time.sleep(1 if _is_captcha_action else 3 * attempt)
                    continue

                ops = result.get("ops", [])
                if not ops:
                    last_error = (
                        f"[Attempt {attempt}] No operations returned from Google Labs."
                    )
                    logger.warning(f"[T2V] Task {task.id}: {last_error}")
                    if attempt < T2V_MAX_ATTEMPTS:
                        time.sleep(3 * attempt)
                    continue

                # Submit thành công — thoát retry loop
                op = ops[0]
                task.op_name = op.get("operation", {}).get("name", "")
                task.scene_id = result.get("scene_id", "")
                store.update_video_task(task)
                # Reset 403 counter on success
                if task.picked_account_name:
                    self._reset_403(task.picked_account_name)
                last_error = None
                break  # Thoát retry loop

            if last_error:
                task.status = TaskStatus.FAILED
                task.error = last_error
                store.update_video_task(task)
                return

            # Safeguard: op_name rỗng → submit chưa OK, không poll
            # (tránh "[Poll] op_name rỗng — submit thất bại" leak vào DB)
            if not task.op_name:
                task.status = TaskStatus.FAILED
                task.error = "T2V: submit thất bại — op_name rỗng sau retry."
                store.update_video_task(task)
                logger.warning(f"[T2V] Task {task.id}: skip poll vì op_name rỗng")
                return

            # Poll & upscale ngay lập tức

            # Poll và download
            file_path = self._poll_and_download(
                task,
                task.op_name,
                task.scene_id,
                prompt,
                client=task_client,
                captcha=captcha,
            )

                # Đã tải xong, kết thúc task ngay

            if file_path:
                task.status = TaskStatus.COMPLETED
                task.completed_at = time.time()
                task.media_id = file_path  # local path hoặc URL
                task.output_filename = os.path.basename(file_path)
                # Reset UNUSUAL_ACTIVITY counter khi thành công
                if task.picked_account_name:
                    self._unusual_activity_counts.pop(task.picked_account_name, None)
                    self._unusual_activity_start_ts.pop(task.picked_account_name, None)
                logger.info(f"[T2V] Task {task.id}: saved as {task.output_filename}")
            else:
                task.status = TaskStatus.FAILED
                task.error = task.error or "Lỗi hệ thống: Không thể tạo video."
            store.update_video_task(task)

        except Exception as e:
            tb = ""
            try:
                import traceback as _tb

                tb = _tb.format_exc()
            except Exception:
                tb = repr(e)
            _filelog(f"[T2V] EXCEPTION task={task.id}: {type(e).__name__}: {e}")
            _filelog(f"[T2V] Traceback: {tb[:500]}")
            logger.error(f"[T2V] Task {task.id}: EXCEPTION: {type(e).__name__}: {e}")
            try:
                task.status = TaskStatus.FAILED
                task.error = f"Lỗi xử lý: {e}"
                store.update_video_task(task)
            except Exception:
                pass
        finally:
            # ── LUÔN clear activity khi task kết thúc (dù thành công hay thất bại) ──
            _acc = getattr(task, "picked_account_name", None)
            if _acc:
                self._clear_activity(_acc)

    # ─── Image-to-Video ───

    def create_image_to_video(
        self,
        project_id: str,
        name: str,
        model: str,
        screen_ratio: str,
        prompts: List[str],
        image_path: str,
        end_image_path: str = None,
        background: bool = True,
    ) -> VideoTask:
        task = VideoTask(
            id=str(uuid.uuid4()),
            project_id=project_id,
            name=name,
            action_type=(
                ActionType.IMAGE_TO_VIDEO
                if not end_image_path
                else ActionType.FRAMES_TO_VIDEO
            ),
            model=model,
            screen_ratio=screen_ratio,
            prompts=prompts,
            status=TaskStatus.PENDING,
        )
        
        # Populate image_refs so the frontend UI displays the image thumbnail correctly
        refs = [{"path": image_path, "name": os.path.basename(image_path)}]
        if end_image_path:
            refs.append({"path": end_image_path, "name": os.path.basename(end_image_path)})
        task.image_refs = refs
        
        task.raw_result = {
            "image_path": image_path,
            "end_image_path": end_image_path,
        }
        store.create_video_task(task)

        if background:
            self.executor.submit(self._run_i2v, task)
        else:
            self._run_i2v(task)

        return task

    def _old_run_i2v_unused(self, task: VideoTask):
        try:
            # ── Timeout check ──
            if self._check_task_timeout(task):
                return
            task.status = TaskStatus.PROCESSING
            store.update_video_task(task)

            raw = task.raw_result or {}

            # Nếu task dùng base64 images (từ UI mới), delegate sang _run_i2v_b64
            if raw.get("images_b64"):
                logger.info(
                    f"[I2V] Task {task.id}: has images_b64, delegating to _run_i2v_b64"
                )
                self._run_i2v_b64(task)
                return

            image_path = raw.get("image_path")
            end_image_path = raw.get("end_image_path")

            if not image_path or not os.path.exists(image_path):
                raise ValidationError(f"Image file not found: {image_path}")

            prompt = task.prompts[0] if task.prompts else ""
            # Resolve I2V model key — đảm bảo dùng r2v model, KHÔNG dùng t2v
            # Nếu task.model là T2V (do server default cũ), force sang I2V_FAST
            _i2v_model = task.model
            if _i2v_model in (VeoModel.T2V_FAST, VeoModel.T2V_FAST_LOW, VeoModel.T2V_QUALITY):
                _i2v_model = VeoModel.I2V_FAST
            model_key = resolve_model_key(_i2v_model, task.screen_ratio)
            logger.info(f"[I2V] Task {task.id}: resolved model_key={model_key} (from task.model={task.model})")
            aspect_str = (
                "IMAGE_ASPECT_RATIO_LANDSCAPE"
                if task.screen_ratio == "16:9"
                else "IMAGE_ASPECT_RATIO_PORTRAIT"
            )

            # --- DYNAMIC CLIENT INITIALIZATION (no-swap, lock account) ---
            failed_accounts = set()  # giữ var cho tương thích, KHÔNG add
            account_locked = None
            task_client = None
            while task_client is None:
                if self._check_task_timeout(task):
                    return
                if account_locked and not task.picked_account_name:
                    task.picked_account_name = account_locked
                task_client = self._resolve_client_for_task(task, exclude_names=set())
                if task_client is None:
                    logger.warning(
                        f"[I2V] Task {task.id}: _resolve_client trả None "
                        f"(locked={account_locked}) → sleep 5s + retry"
                    )
                    time.sleep(5)
                    continue
                if not account_locked:
                    account_locked = task.picked_account_name or getattr(task_client, "_account_label", None)
                    if account_locked:
                        task.picked_account_name = account_locked
                        _filelog(f"[I2V] Task {task.id}: LOCKED account={account_locked} (no-swap)")
                        threading.Thread(
                            target=task_client.warmup_browser_runtime,
                            name=f"veo-warmup-{task.id[:8]}",
                            daemon=True,
                        ).start()

            # Solve captcha — qua Extension (Chrome thật)
            task_client._account_label = task.picked_account_name

            # Tạo Google Labs project TRƯỚC (không cần captcha)
            # 🆕 Ưu tiên shared_gl_project từ pre-upload phase
            gl_project = raw.get("shared_gl_project")
            if gl_project:
                logger.info(
                    f"[I2V] Task {task.id}: dùng shared GL project từ pre-upload: {gl_project[:20]}..."
                )
            else:
                try:
                    from core.project import create_project, search_user_projects

                    # Ưu tiên lấy project mới nhất đã có
                    try:
                        _existing = search_user_projects(
                            cookie=task_client.cookie,
                            access_token=task_client.access_token,
                            page_size=1,
                            timeout=8,
                            proxy=task_client.proxy,
                        )
                        if _existing and len(_existing) > 0:
                            gl_project = _existing[0].get("projectId")
                            if gl_project:
                                logger.info(
                                    f"[I2V] Reusing existing GL project: {gl_project[:20]}..."
                                )
                    except Exception as _se:
                        logger.warning(f"[I2V] search_user_projects failed: {_se}")

                    # Fallback: tạo mới
                    if not gl_project:
                        gl_project = create_project(
                            f"API-{task.project_id[:8]}",
                            tool_name="PINHOLE",
                            cookie=task_client.cookie,
                            access_token=task_client.access_token,
                            browser_headers=task_client.base_headers,
                            proxy=task_client.proxy,
                        )
                    if gl_project:
                        logger.info(f"[Project] GL project ready: {gl_project[:20]}...")
                except Exception as e:
                    logger.warning(f"[Project] Could not get/create Google Labs project: {e}")
                    gl_project = task.project_id

            # Set project ID trên client để Referer chính xác
            task_client._current_project_id = gl_project

            # Upload start image TRƯỚC captcha (mất 5-15s, captcha sẽ tươi hơn)
            aspect_str_upload = (
                "IMAGE_ASPECT_RATIO_LANDSCAPE"
                if task.screen_ratio == "16:9"
                else "IMAGE_ASPECT_RATIO_PORTRAIT"
            )

            # ── Upload với retry vô hạn trên CÙNG account (rule no-swap) ──
            # Timeout 1200s là guard duy nhất; mỗi vòng kiểm tra _check_task_timeout.
            start_id = None
            end_id = None
            _upload_attempt = 0
            while True:
                _upload_attempt += 1
                # Timeout guard
                if self._check_task_timeout(task):
                    return
                logger.info(f"[I2V] Task {task.id}: uploading start image (attempt {_upload_attempt})")
                start_id, _err_class = self._upload_with_proxy_retry(
                    task_client,
                    task_client.upload_image_from_path,
                    image_path,
                    aspect=aspect_str_upload,
                    project_id=gl_project,
                    flow="I2V-start",
                    task_id=task.id,
                )

                if start_id:
                    # Upload thành công → reset 401 counter cho account
                    _acc_name = task.picked_account_name
                    if _acc_name:
                        self._account_upload_401_fails.pop(_acc_name, None)
                    break

                # Upload thất bại → log + xoay proxy + reload cookie + retry SAME account
                _upload_status = getattr(task_client, '_last_upload_status', None)
                _acc_name = task.picked_account_name
                logger.warning(
                    f"[I2V] Upload failed (status={_upload_status} err={_err_class}) "
                    f"account={_acc_name} attempt={_upload_attempt} → retry SAME account "
                    f"(rule no-swap, chỉ timeout 1200s mới cancel)"
                )

                if _err_class == "auth" and _acc_name:
                    # 401 → tăng counter (chỉ để log)
                    _cnt = self._account_upload_401_fails.get(_acc_name, 0) + 1
                    self._account_upload_401_fails[_acc_name] = _cnt
                    # Reload cookie từ DB (extension push token mới)
                    try:
                        _fresh_acc = store.get_veo_account_by_name(_acc_name)
                        if _fresh_acc and _fresh_acc.cookie:
                            resolved_proxy = self._resolve_proxy_for_account(_fresh_acc)
                            from core.hybrid_veo_client import HybridVeoClient as _HVC
                            task_client = _HVC(_fresh_acc.cookie, proxy=resolved_proxy, account_email=_fresh_acc.name)
                            task_client._account_label = _acc_name
                            task_client._current_project_id = gl_project
                            if task_client.access_token:
                                logger.info(f"[I2V] ✅ Cookie mới cho {_acc_name} → retry upload SAME account")
                    except Exception as _re:
                        logger.warning(f"[I2V] Reload cookie error: {_re}")

                # Mark proxy hiện tại là dead + lấy fresh từ static pool (200+ proxy)
                # KHÔNG rotate KiotProxy (gây ban IP).
                if _acc_name:
                    _old_proxy = getattr(task_client, "proxy", None)
                    new_ip = None
                    try:
                        from core.static_proxy_pool import get_fresh_proxy as _gfp, mark_proxy_dead as _mpd
                        if _old_proxy:
                            _mpd(_old_proxy)
                        new_ip = _gfp(exclude={_old_proxy} if _old_proxy else None)
                    except Exception:
                        new_ip = None
                    if new_ip:
                        try:
                            task_client.proxy = new_ip
                        except Exception:
                            pass
                        logger.info(f"[I2V] Upload retry trên {_acc_name} với proxy fresh từ pool: {new_ip[:40]}")
                time.sleep(2)

            if not start_id:
                # Chỉ vào đây nếu vòng while thoát do timeout (đã set FAILED + return).
                # Defensive guard.
                return

            # Upload end image nếu có. Theo MAU.MD: đã có ảnh cuối thì bắt buộc
            # route start/end, KHÔNG fallback sang single-image vì sẽ sai ý người dùng.
            if end_image_path:
                if not os.path.exists(end_image_path):
                    task.status = TaskStatus.FAILED
                    task.error = f"End image file not found: {end_image_path}"
                    logger.error(f"[I2V] Task {task.id}: {task.error}")
                    store.update_video_task(task)
                    return

                logger.info(f"[I2V] Task {task.id}: uploading end image")
                end_id, _end_err = self._upload_with_proxy_retry(
                    task_client,
                    task_client.upload_image_from_path,
                    end_image_path,
                    aspect=aspect_str_upload,
                    project_id=gl_project,
                    flow="I2V-end",
                    task_id=task.id,
                )
                if not end_id:
                    task.status = TaskStatus.FAILED
                    task.error = (
                        f"Upload end image failed (err={_end_err}); "
                        "start/end video requires both start and end frames"
                    )
                    logger.error(f"[I2V] Task {task.id}: {task.error}")
                    store.update_video_task(task)
                    return

            # Upload xong cả 2 ảnh → bắt đầu giải captcha ngay

            # Giải captcha SAU upload — retry vô hạn (no-swap, no-fail).
            # Timeout 1200s là guard duy nhất.
            captcha = ""
            _cap_try = 0
            while not captcha:
                if self._check_task_timeout(task):
                    return
                _cap_try += 1
                captcha = self._solve_captcha_smart(task_client, "VIDEO_GENERATION")
                if captcha:
                    break
                logger.warning(
                    f"[I2V] Task {task.id}: captcha attempt {_cap_try} failed → sleep 2s + retry"
                )
                time.sleep(2)

            # (project creation, upload, captcha đã xử lý ở trên)

            # Submit with retry for captcha errors
            I2V_CAPTCHA_MAX_RETRIES = 999999  # Không giới hạn vòng lặp, chỉ phụ thuộc timeout
            _i2v_captcha_attempt = 0
            result = None
            while _i2v_captcha_attempt < I2V_CAPTCHA_MAX_RETRIES:
                _i2v_captcha_attempt += 1
                # ── TIMEOUT CHECK mỗi iteration ──
                if self._check_task_timeout(task):
                    logger.warning(
                        f"[I2V] Task {task.id}: TIMEOUT → auto-cancelled "
                        f"(captcha_attempt={_i2v_captcha_attempt})"
                    )
                    self._release_cookie(task.picked_account_name or "")
                    self._clear_activity(task.picked_account_name or "")
                    return
                # Main API flow: do not serialize create per account.
                # Worker/thread count and token distribution control parallelism.
                self._set_activity(task.picked_account_name or "", "CALLING_API", task.id)
                if end_id:
                    logger.info(f"[I2V] Task {task.id}: start+end frame mode (captcha attempt {_i2v_captcha_attempt}/{I2V_CAPTCHA_MAX_RETRIES})")
                    result = task_client.create_video_start_end_image(
                        row=0,
                        prompt=prompt,
                        project_id=gl_project,
                        captcha_token=captcha,
                        start_image_media_id=start_id,
                        end_image_media_id=end_id,
                        aspect=task.screen_ratio,
                    )
                else:
                    logger.info(f"[I2V] Task {task.id}: single image mode (captcha attempt {_i2v_captcha_attempt}/{I2V_CAPTCHA_MAX_RETRIES})")
                    result = task_client.create_video_i2v(
                        row=0,
                        prompt=prompt,
                        project_id=gl_project,
                        captcha_token=captcha,
                        start_image_media_id=start_id,
                        aspect=task.screen_ratio,
                        model_key=model_key,
                    )

                if result:
                    break  # Success

                api_detail_raw = (
                    getattr(task_client, "_last_error_detail", None) or "no detail"
                )
                api_detail = api_detail_raw
                # ── Banana §06: Centralized error handling ──
                _err_rv = self._handle_api_error_banana(
                    api_detail=api_detail_raw,
                    account_label=task.picked_account_name or "",
                    flow="I2V",
                    task_client=task_client,
                    captcha_action="VIDEO_GENERATION",
                )

                if _err_rv["should_fail_task"]:
                    task.status = TaskStatus.FAILED
                    task.error = _err_rv["fail_error"]
                    store.update_video_task(task)
                    return

                if _err_rv["new_task_client"]:
                    task_client = _err_rv["new_task_client"]
                if _err_rv["new_captcha"]:
                    captcha = _err_rv["new_captcha"]

                if not _err_rv["should_retry"]:
                    api_detail = api_detail_raw
                    break
                continue

            if not result:
                task.status = TaskStatus.FAILED
                task.error = f"Lỗi gửi yêu cầu ảnh sang video: {api_detail}"
                logger.error(f"[I2V] Task {task.id}: {task.error}")
                store.update_video_task(task)
                return

            ops = result.get("ops", [])
            op = ops[0] if ops else {}
            task.op_name = op.get("operation", {}).get("name", "")
            task.scene_id = result.get("scene_id", "")
            store.update_video_task(task)

            # Poll & upscale ngay lập tức

            file_path = self._poll_and_download(
                task,
                task.op_name,
                task.scene_id,
                prompt,
                client=task_client,
                captcha=captcha,
            )

                # Đã tải xong, kết thúc task ngay
            if file_path:
                task.status = TaskStatus.COMPLETED
                task.media_id = file_path
                # Reset UNUSUAL_ACTIVITY counter khi thành công
                if task.picked_account_name:
                    self._unusual_activity_counts.pop(task.picked_account_name, None)
                    self._unusual_activity_start_ts.pop(task.picked_account_name, None)
                # GIỮ LẠI ảnh gốc để retry hoạt động — không xóa ngay
                # Ảnh sẽ được cleanup bởi scheduled job hoặc admin thủ công
                for img_p in [image_path, end_image_path]:
                    if img_p and os.path.exists(img_p):
                        logger.info(f"[I2V] Giữ lại ảnh gốc cho retry: {img_p}")
            else:
                task.status = TaskStatus.FAILED
                task.error = task.error or "Lỗi hệ thống: Không thể xử lý video."
            store.update_video_task(task)

        except Exception as e:
            import traceback

            tb = traceback.format_exc()
            logger.exception(f"[I2V] Task {task.id}: error: {e}")
            task.status = TaskStatus.FAILED
            task.error = f"Lỗi không mong muốn: {e}"
            store.update_video_task(task)
        finally:
            # ── LUÔN clear activity khi task kết thúc ──
            _acc = getattr(task, "picked_account_name", None)
            if _acc:
                self._clear_activity(_acc)

    def _old_run_i2v_b64_unused(self, task: VideoTask):
        """I2V từ browser base64 upload — KHÔNG đụng vào _run_i2v cũ."""
        try:
            # ── Timeout check ──
            if self._check_task_timeout(task):
                return
            task.status = TaskStatus.PROCESSING
            store.update_video_task(task)

            raw = task.raw_result or {}
            images_b64 = raw.get(
                "images_b64", []
            )  # [{b64, mime, name} hoặc {path, mime, name}]

            # Xóa cache_mid từ Database lưu lại (do user bấm Retry) để tránh lỗi 404
            for img in images_b64:
                img.pop("cached_mid", None)

            mode = raw.get("mode", "single")  # "single" | "frames"

            if not images_b64:
                task.status = TaskStatus.FAILED
                task.error = "Không có ảnh nào được cung cấp."
                store.update_video_task(task)
                return

            prompt = task.prompts[0] if task.prompts else ""
            # Resolve I2V model key — đảm bảo dùng r2v model, KHÔNG dùng t2v
            _i2v_model = task.model
            if _i2v_model in (VeoModel.T2V_FAST, VeoModel.T2V_FAST_LOW, VeoModel.T2V_QUALITY):
                _i2v_model = VeoModel.I2V_FAST
            model_key = resolve_model_key(_i2v_model, task.screen_ratio)
            logger.info(f"[I2V-B64] Task {task.id}: resolved model_key={model_key} (from task.model={task.model})")
            aspect_str = (
                "IMAGE_ASPECT_RATIO_PORTRAIT"
                if task.screen_ratio == "9:16"
                else "IMAGE_ASPECT_RATIO_LANDSCAPE"
            )
            video_aspect = (
                "VIDEO_ASPECT_RATIO_PORTRAIT"
                if task.screen_ratio == "9:16"
                else "VIDEO_ASPECT_RATIO_LANDSCAPE"
            )

            # NO-SWAP: I2V-B64 retry vô hạn cùng account. Lỗi F5-recoverable
            # (UNUSUAL_ACTIVITY 403, 429 too much, RESOURCE_EXHAUSTED, 5xx) →
            # F5 + retry. Chỉ timeout 1200s cancel.
            I2V_MAX_ATTEMPTS = 999999
            failed_accounts = set()  # giữ var cho tương thích, KHÔNG add
            last_error = None
            result = None
            task_client = None
            captcha = ""
            _prev_account = None
            account_locked = None  # email account khoá cứng cho task này

            for attempt in range(1, I2V_MAX_ATTEMPTS + 1):
                # ── Timeout guard duy nhất ──
                if self._check_task_timeout(task):
                    return
                # ── Check: task có bị PAUSED giữa chừng không? ──
                _fresh_i = store.get_video_task(task.id)
                if _fresh_i and str(getattr(_fresh_i, 'status', '')).upper() == 'PAUSED':
                    logger.info(f"[I2V-B64] ⏸️ Task {task.id} đã bị PAUSED → dừng retry")
                    return

                logger.info(
                    f"[I2V-B64] Task {task.id}: attempt {attempt} (no-swap, locked={account_locked})"
                )

                # Lock account: lần đầu pick → set vào account_locked, các lần
                # sau buộc dùng đúng account đó.
                if account_locked and not task.picked_account_name:
                    task.picked_account_name = account_locked
                task_client = self._resolve_client_for_task(
                    task, exclude_names=set()  # KHÔNG exclude → giữ cùng account
                )
                if task_client is None:
                    # Account locked tạm thời chưa available → sleep + retry,
                    # KHÔNG break (timeout 1200s là guard duy nhất).
                    logger.warning(
                        f"[I2V-B64] Task {task.id}: _resolve_client trả None "
                        f"(locked={account_locked}) → sleep 5s + retry"
                    )
                    time.sleep(5)
                    continue
                # Lock account ở lần pick đầu
                if not account_locked:
                    account_locked = task.picked_account_name or getattr(task_client, "_account_label", None)
                    if account_locked:
                        task.picked_account_name = account_locked
                        _filelog(f"[I2V-B64] Task {task.id}: LOCKED account={account_locked} (no-swap)")
                        threading.Thread(
                            target=task_client.warmup_browser_runtime,
                            name=f"veo-warmup-{task.id[:8]}",
                            daemon=True,
                        ).start()

                # ── Nếu account thay đổi → xoá cached media IDs (thuộc GL project cũ) ──
                _cur_account = task.picked_account_name
                if _prev_account and _cur_account != _prev_account:
                    for img in images_b64:
                        img.pop("cached_mid", None)
                    logger.info(
                        f"[I2V-B64] Account changed {_prev_account} → {_cur_account}, cleared cached media IDs"
                    )
                _prev_account = _cur_account

                task_client._account_label = task.picked_account_name

                # ── Bước 1: Tạo GL project ──
                gl_project = task.project_id
                try:
                    from core.project import create_project, search_user_projects

                    # Ưu tiên lấy project mới nhất đã có
                    proj = None
                    try:
                        _existing = search_user_projects(
                            cookie=task_client.cookie,
                            access_token=task_client.access_token,
                            page_size=1,
                            timeout=8,
                            proxy=task_client.proxy,
                        )
                        if _existing and len(_existing) > 0:
                            proj = _existing[0].get("projectId")
                            if proj:
                                logger.info(
                                    f"[I2V-B64] Reusing existing GL project: {proj[:20]}..."
                                )
                    except Exception as _se:
                        logger.warning(f"[I2V-B64] search_user_projects failed: {_se}")

                    # Fallback: tạo mới
                    if not proj:
                        proj = create_project(
                            f"API-{task.project_id[:8]}",
                            tool_name="PINHOLE",
                            cookie=task_client.cookie,
                            access_token=task_client.access_token,
                            browser_headers=task_client.base_headers,
                        )
                    if proj:
                        gl_project = proj
                except Exception as e:
                    logger.warning(f"[I2V-B64] Could not get/create GL project: {e}")

                # ── Bước 2: Upload ảnh TRƯỚC (Parallel Upload) ──
                media_ids = []
                upload_failed = False
                file_missing_path = None
                
                def _up_b64_task(img_item):
                    nonlocal file_missing_path
                    try:
                        mid = img_item.get("cached_mid")
                        if mid: return mid

                        path = img_item.get("path", "")
                        b64 = img_item.get("b64", "")
                        fname = img_item.get("name", "upload.jpg")
                        mime = img_item.get("mime", "image/jpeg")

                        if path:
                            if os.path.exists(path):
                                _mid, _ = self._upload_with_proxy_retry(
                                    task_client,
                                    task_client.upload_image_from_path,
                                    path,
                                    aspect=aspect_str,
                                    project_id=gl_project,
                                    flow="I2V-B64-path",
                                    task_id=task.id,
                                )
                                return _mid
                            else:
                                file_missing_path = path
                                return None
                        elif b64:
                            _mid, _ = self._upload_with_proxy_retry(
                                task_client,
                                task_client.upload_user_image,
                                b64,
                                mime=mime,
                                aspect=aspect_str,
                                project_id=gl_project,
                                file_name=fname,
                                flow="I2V-B64-b64",
                                task_id=task.id,
                            )
                            return _mid
                    except Exception: pass
                    return None

                # Upload song song tối đa 5 ảnh (thường I2V chỉ có 1-2 ảnh)
                with _futures.ThreadPoolExecutor(max_workers=5) as executor:
                    results = list(executor.map(_up_b64_task, images_b64))
                
                for idx, mid in enumerate(results):
                    if mid:
                        images_b64[idx]["cached_mid"] = mid
                        media_ids.append(mid)
                    else:
                        if not file_missing_path: upload_failed = True

                if file_missing_path:
                    task.status = TaskStatus.FAILED
                    task.error = f"⚠ File ảnh gốc không tồn tại: {file_missing_path}"
                    store.update_video_task(task)
                    return


                if upload_failed or not media_ids:
                    bad_acc = task.picked_account_name
                    _upload_status = getattr(task_client, '_last_upload_status', None)
                    # Capture full upload diagnostic từ client (đã được enhance trong veo_client.py)
                    _upload_diag = getattr(task_client, '_last_upload_diag', None)
                    # Lưu vào task.raw_result để user debug sau
                    if _upload_diag:
                        try:
                            if not isinstance(task.raw_result, dict):
                                task.raw_result = {}
                            _diag_list = task.raw_result.get("upload_401_diag") or []
                            # Chỉ lưu 5 diag gần nhất để không phình task quá nhiều
                            _diag_list.append({
                                "attempt": attempt,
                                "timestamp": int(time.time()),
                                **{k: v for k, v in _upload_diag.items() if k != "attempts"},
                                "try_count": len(_upload_diag.get("attempts", [])),
                                "try1_status": (_upload_diag.get("attempts") or [{}])[0].get("status"),
                                "try2_status": (
                                    (_upload_diag.get("attempts") or [{}, {}])[1].get("status")
                                    if len(_upload_diag.get("attempts") or []) >= 2 else None
                                ),
                                "try2_token_changed": (
                                    (_upload_diag.get("attempts") or [{}, {}])[1].get("token_changed_from_try1")
                                    if len(_upload_diag.get("attempts") or []) >= 2 else None
                                ),
                                "last_body_preview": (
                                    (_upload_diag.get("attempts") or [{}])[-1].get("body_preview", "")
                                )[:300],
                            })
                            task.raw_result["upload_401_diag"] = _diag_list[-5:]
                        except Exception:
                            pass
                    if bad_acc:
                        # RULE NO-SWAP: upload fail → reload cookie từ DB + xoay proxy
                        # từ pool, retry SAME account. KHÔNG swap, KHÔNG FAIL — chỉ timeout
                        # 1200s mới cancel.

                        # 1. 401 → tăng counter (chỉ để log, KHÔNG blacklist+swap nữa)
                        if _upload_status == 401:
                            _cnt = self._account_upload_401_fails.get(bad_acc, 0) + 1
                            self._account_upload_401_fails[bad_acc] = _cnt
                            logger.warning(
                                f"[I2V-B64] Upload 401 on {bad_acc} (lần {_cnt}) → "
                                f"reload cookie + xoay proxy → retry SAME account"
                            )
                        else:
                            logger.warning(
                                f"[I2V-B64] Upload fail status={_upload_status} on {bad_acc} → "
                                f"xoay proxy → retry SAME account"
                            )

                        # 2. Reload cookie từ DB (extension push token mới liên tục)
                        try:
                            _fresh_acc = store.get_veo_account_by_name(bad_acc)
                            if _fresh_acc and _fresh_acc.cookie:
                                _proxy_for_reload = self._resolve_proxy_for_account(_fresh_acc)
                                from core.hybrid_veo_client import HybridVeoClient as _HVC
                                _new_client = _HVC(_fresh_acc.cookie, proxy=_proxy_for_reload, account_email=_fresh_acc.name)
                                if _new_client.access_token:
                                    task_client = _new_client
                                    setattr(task_client, "_account_label", bad_acc)
                                    logger.info(
                                        f"[I2V-B64] ✅ Reload cookie {bad_acc} OK"
                                    )
                        except Exception as _re:
                            logger.warning(f"[I2V-B64] Reload cookie error: {_re}")

                        # 3. Xoay proxy từ static pool (200+ proxy) — KHÔNG rotate
                        # KiotProxy (gây ban IP gọi KiotProxy API).
                        _old_proxy = getattr(task_client, "proxy", None)
                        new_ip = None
                        try:
                            from core.static_proxy_pool import get_fresh_proxy as _gfp, mark_proxy_dead as _mpd
                            if _old_proxy:
                                _mpd(_old_proxy)
                            new_ip = _gfp(exclude={_old_proxy} if _old_proxy else None)
                        except Exception:
                            new_ip = None
                        if new_ip:
                            try:
                                task_client.proxy = new_ip
                            except Exception:
                                pass
                            logger.info(
                                f"[I2V-B64] Upload retry trên {bad_acc} với proxy fresh từ pool: {new_ip[:40]}"
                            )

                    last_error = f"[Attempt {attempt}] Upload failed (status={_upload_status}) account={bad_acc} → retry SAME account."
                    time.sleep(2)
                    continue

                # Bỏ sleep(15) sau upload

                # ── Bước 3+4: Giải captcha + Gọi API (inner retry cho reCAPTCHA 403) ──
                # Khi reCAPTCHA 403 → chỉ lấy token mới + gọi API lại
                # KHÔNG cần upload lại (đã có media_ids), KHÔNG swap account
                CAPTCHA_INNER_MAX = 999999  # Không giới hạn vòng lặp, chỉ phụ thuộc timeout
                _captcha_retry = 0
                _api_success = False

                for _captcha_retry in range(CAPTCHA_INNER_MAX):
                    # ── TIMEOUT CHECK mỗi iteration ──
                    if self._check_task_timeout(task):
                        logger.warning(
                            f"[I2V-B64] Task {task.id}: TIMEOUT → auto-cancelled "
                            f"(captcha_retry={_captcha_retry})"
                        )
                        self._release_cookie(task.picked_account_name or "")
                        self._clear_activity(task.picked_account_name or "")
                        return
                    # ── Lấy captcha token (tươi nhất) ──
                    captcha = self._solve_captcha_smart(
                        task_client,
                        "VIDEO_GENERATION",
                    )

                    if not captcha:
                        logger.warning(
                            f"[I2V-B64] Attempt {attempt}, captcha try {_captcha_retry+1}/{CAPTCHA_INNER_MAX}: "
                            f"captcha empty → thử lại"
                        )
                        last_error = f"[Attempt {attempt}] Captcha failed (empty token)."
                        if _captcha_retry + 1 < CAPTCHA_INNER_MAX:
                            time.sleep(2)
                        continue

                    # Main API flow: do not serialize create per account.
                    # Worker/thread count and token distribution control parallelism.
                    self._set_activity(task.picked_account_name or "", "CALLING_API", task.id)
                    if len(media_ids) >= 2:
                        logger.info(
                            f"[I2V-B64] Frames mode (captcha try {_captcha_retry+1}): "
                            f"start={media_ids[0][:20]}, end={media_ids[1][:20]}"
                        )
                        result = task_client.create_video_start_end_image(
                            row=0,
                            prompt=prompt,
                            project_id=gl_project,
                            captcha_token=captcha,
                            start_image_media_id=media_ids[0],
                            end_image_media_id=media_ids[1],
                            aspect=video_aspect,
                        )
                    else:
                        logger.info(
                            f"[I2V-B64] Single mode (captcha try {_captcha_retry+1}): "
                            f"start={media_ids[0][:20]}"
                        )
                        result = task_client.create_video_i2v(
                            row=0,
                            prompt=prompt,
                            project_id=gl_project,
                            captcha_token=captcha,
                            start_image_media_id=media_ids[0],
                            aspect=video_aspect,
                            model_key=model_key,
                        )

                    if result:
                        _api_success = True
                        break  # ✅ API thành công

                    # ── API thất bại → phân loại lỗi ──
                    api_detail = (
                        getattr(task_client, "_last_error_detail", None) or "no detail"
                    )
                    short_detail = api_detail[:150].replace("\n", " ") + (
                        "..." if len(api_detail) > 150 else ""
                    )

                    # ── Banana §06: Centralized error handling ──
                    _err_rv = self._handle_api_error_banana(
                        api_detail=api_detail,
                        account_label=task.picked_account_name or "",
                        flow="I2V-B64",
                        task_client=task_client,
                        captcha_action="VIDEO_GENERATION",
                    )

                    if _err_rv["should_fail_task"]:
                        task.status = TaskStatus.FAILED
                        task.error = _err_rv["fail_error"]
                        store.update_video_task(task)
                        return

                    if _err_rv["new_task_client"]:
                        task_client = _err_rv["new_task_client"]
                    if _err_rv["new_captcha"]:
                        captcha = _err_rv["new_captcha"]

                    if not _err_rv["should_retry"]:
                        last_error = f"Lỗi gửi yêu cầu tạo video (Lần {attempt}). {short_detail}"
                        break
                    continue

                if _api_success:
                    last_error = None
                    if task.picked_account_name:
                        self._reset_403(task.picked_account_name)
                    break  # Thoát outer loop → thành công

            if last_error or result is None:
                task.status = TaskStatus.FAILED
                task.error = last_error or "Tất cả attempt đều thất bại (không có kết quả từ API)."
                store.update_video_task(task)
                return

            ops = result.get("ops", [])
            op = ops[0] if ops else {}
            task.op_name = op.get("operation", {}).get("name", "")
            task.scene_id = result.get("scene_id", "")
            store.update_video_task(task)

            # Poll & upscale ngay lập tức

            file_path = self._poll_and_download(
                task,
                task.op_name,
                task.scene_id,
                prompt,
                client=task_client,
                captcha=captcha,
            )

                # Đã tải xong, kết thúc task ngay
            if file_path:
                task.status = TaskStatus.COMPLETED
                task.media_id = file_path
                # GIỮ LẠI ảnh gốc để hỗ trợ retry sau này
                # (Trước đây xóa ảnh ở đây gây lỗi khi retry I2V)
                # for img in images_b64:
                #     p = img.get("path")
                #     if p and os.path.exists(p):
                #         os.remove(p)
            else:
                task.status = TaskStatus.FAILED
                task.error = task.error or "I2V (b64) generation failed."
            store.update_video_task(task)

        except Exception as e:
            import traceback

            tb = traceback.format_exc()
            logger.exception(f"[I2V-B64] Task {task.id}: error: {e}")
            task.status = TaskStatus.FAILED
            task.error = tb
            store.update_video_task(task)
        finally:
            # ── LUÔN clear activity khi task kết thúc ──
            _acc = getattr(task, "picked_account_name", None)
            if _acc:
                self._clear_activity(_acc)

    def create_frames_to_video(
        self,
        project_id: str,
        name: str,
        model: str,
        screen_ratio: str,
        prompts: List[str],
        start_image_path: str,
        end_image_path: str,
        background: bool = True,
    ) -> VideoTask:
        """Shorthand: I2V với cả start và end frame."""
        return self.create_image_to_video(
            project_id=project_id,
            name=name,
            model=model,
            screen_ratio=screen_ratio,
            prompts=prompts,
            image_path=start_image_path,
            end_image_path=end_image_path,
            background=background,
        )

    # ─── Get task ───

    def get_task(self, task_id: str) -> VideoTask:
        task = store.get_video_task(task_id)
        if not task:
            raise TaskNotFoundError(task_id)
        return task

    # ─── Image Generation ───

    def create_image(
        self,
        project_id: str,
        name: str,
        prompt: str,
        model: str = "NARWHAL",
        aspect: str = "IMAGE_ASPECT_RATIO_PORTRAIT",
        count: int = 1,
        image_refs: list = None,
        veo_cookie: str = None,
        proxy_url: str = None,
        task=None,
    ) -> "VideoTask":
        """Public entry-point: chạy tạo ảnh cho task đã có sẵn."""
        if task is None:
            from web.models import ActionType

            task = VideoTask(
                id=str(__import__("uuid").uuid4()),
                project_id=project_id,
                name=name,
                action_type=ActionType.CREATE_IMAGE,
                model=model,
                screen_ratio=aspect,
                prompts=[prompt],
                status=TaskStatus.PENDING,
                veo_cookie=veo_cookie,
                proxy_url=proxy_url,
                image_refs=image_refs,
            )
            store.create_video_task(task)
        # Enqueue vào VEO queue
        self.enqueue(task, self._run_create_image)
        return task

    # ─── NanoAI Upscale Fallback ───────────────────────────────────
    def _call_nanoai_upscale(self, access_token: str, media_id: str,
                              project_id: str, resolution: str = "4K") -> "bytes | None":
        """Fallback upscale qua NanoAI khi Google upsample thất bại.
        Chỉ dùng khi user truyền upsample_resolution = '4K'.
        """
        import requests as _req
        import base64 as _b64
        import time as _time

        # 1. Lấy cấu hình
        NANO_TOKEN = store.get_setting("nanoai_token", "") or ""
        if not NANO_TOKEN:
            _filelog("[NanoAI] ⚠ Chưa cấu hình NanoAI token → bỏ qua fallback")
            return None

        # 2. Chuẩn bị Request
        NANO_URL = "https://flow-api.nanoai.pics/api/v2/images/upscale"
        POLL_URL_BASE = "https://flow-api.nanoai.pics/api/fix/task-status"
        
        _res_map = {"4K": "RESOLUTION_4K", "2K": "RESOLUTION_2K"}
        target_res = _res_map.get(resolution.upper(), "RESOLUTION_4K")

        # Extract plain UUID từ media_id (bỏ prefix "projects/.../media/")
        _mid = str(media_id) if media_id else ""
        if "/" in _mid:
            _mid = _mid.rsplit("/", 1)[-1]

        payload = {
            "accessToken": access_token,
            "mediaId": _mid,
            "projectId": project_id,
            "targetResolution": target_res,
        }
        headers = {
            "Authorization": f"Bearer {NANO_TOKEN}",
            "Content-Type": "application/json",
        }


        # BƯỚC 1: Gửi yêu cầu Upscale (Thử tối đa 3 lần nếu chưa có ảnh ngay)
        for attempt in range(3):
            try:
                _filelog(f"[NanoAI] 🚀 Khởi tạo Upscale (Lần {attempt+1}): mediaId={_mid[:15]}... res={target_res}")
                resp = _req.post(NANO_URL, json=payload, headers=headers, timeout=120)
                if resp.status_code != 200:
                    _filelog(f"[NanoAI] ❌ Request failed: {resp.status_code}")
                    return None
                
                data = resp.json()
                if not data.get("success"):
                    _filelog(f"[NanoAI] ❌ Server trả về lỗi: {data.get('message', 'Unknown error')}")
                    return None

                # BƯỚC 2: Kiểm tra kết quả trả về ngay
                result_obj = data.get("result") or {}
                if result_obj.get("encodedImage"):
                    img_bytes = _b64.b64decode(result_obj["encodedImage"])
                    _filelog(f"[NanoAI] ✅ Thành công ({len(img_bytes)} bytes)")
                    return img_bytes
                
                # Nếu chưa có ảnh, chờ 5s rồi thử lại
                _filelog(f"[NanoAI] ⏳ Chưa có ảnh ngay, chờ 5s rồi thử lại lần {attempt+2}...")
                _time.sleep(5)
            except Exception as e:
                _filelog(f"[NanoAI] ⚠ Lỗi vòng {attempt+1}: {e}")
                _time.sleep(5)
        
        _filelog(f"[NanoAI] ❌ Thử 3 lần vẫn không nhận được encodedImage")
        return None



    def _run_banana_task(self, task: VideoTask):
        try:  # OUTER guard: catches ALL exceptions including OSError from console
            # CREATE_IMAGE phải chạy qua reference-implementation, kể cả task cũ bị retry/re-enqueue.
            # Không gọi core.banana_adapter/core.banana_runtime tại đây nữa.
            try:
                from web.account_session_api import AccountSessionError, account_session_api
                from core.reference_runtime import run_reference_jobs

                _output_dir = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "output",
                )
                _configured_threads = int(store.get_setting("banana_thread_count", "10") or 10)
                _max_attempts = int(store.get_setting("banana_max_attempts", "5") or 5)

                _user = None
                _api_key = (getattr(task, "veo_cookie", None) or "").strip()
                if _api_key:
                    try:
                        _user = store.get_user_by_api_key(_api_key)
                    except Exception:
                        _user = None
                if not _user:
                    _users = list(store.list_users())
                    _user = next((u for u in _users if getattr(u, "is_active", False)), None)
                if not _user:
                    raise RuntimeError("No active API user available for account-session")

                _picked_account_name = (getattr(task, "picked_account_name", None) or getattr(task, "pinned_account_name", None) or "").strip()
                try:
                    if _picked_account_name:
                        _session = account_session_api.issue_for_account_name(_picked_account_name, user=_user)
                    else:
                        _session = account_session_api.issue_for_user(_user)
                except AccountSessionError as _session_error:
                    _code = getattr(_session_error, "code", "") or ""
                    _message = str(_session_error)
                    _bad_account = _picked_account_name or (getattr(task, "picked_account_name", None) or getattr(task, "pinned_account_name", None) or "")
                    if _bad_account and _code in ("SESSION_UNAVAILABLE", "ACCOUNT_UNAVAILABLE"):
                        _filelog(
                            f"[ReferenceImplementation] session failed task={task.id} account={_bad_account} "
                            f"code={_code} error={_message} → blacklist account_project, clear pin, requeue"
                        )
                        self._account_project_blacklist.add(_bad_account)
                        try:
                            task.picked_account_name = None
                            task.pinned_account_name = None
                            task._pinned_busy_requeue = True
                            store.update_video_task(task)
                        except Exception:
                            pass
                        self.enqueue(task, self._run_banana_task)
                        return
                    raise
                _token = (_session.get("token") or "").strip()
                _project_id = (_session.get("project_id") or "").strip()
                _session_account = (_session.get("account_name") or "").strip()
                if _picked_account_name and _session_account != _picked_account_name:
                    raise RuntimeError(
                        f"Account session mismatch: queue={_picked_account_name} session={_session_account}"
                    )
                if not _token or not _project_id:
                    raise RuntimeError("account-session did not return token/project_id")
                _token_sha8 = hashlib.sha1(_token.encode("utf-8", errors="ignore")).hexdigest()[:8]
                logger.info(
                    "[ReferenceImplementation] session account=%s queue_account=%s token=%s project=%s task=%s",
                    _session_account,
                    _picked_account_name or "",
                    _token_sha8,
                    _project_id,
                    getattr(task, "id", ""),
                )
                _filelog(
                    f"[ReferenceImplementation] bound account task={task.id} "
                    f"queue_account={_picked_account_name or 'user-assigned'} "
                    f"session_account={_session_account} token={_token_sha8} project={_project_id}"
                )

                _credit_info = _reference_check_token_credit_local(_token, "", _project_id)
                if not _credit_info.get("ok"):
                    logger.warning(
                        "[ReferenceImplementation] credit-check primary skip account=%s token=%s error=%s task=%s",
                        _session_account,
                        _credit_info.get("token_fingerprint"),
                        _credit_info.get("error"),
                        getattr(task, "id", ""),
                    )
                    _filelog(
                        f"[ReferenceImplementation] credit-check primary skip task={task.id} "
                        f"account={_session_account} token={_credit_info.get('token_fingerprint')} "
                        f"error={_credit_info.get('error')}"
                    )
                    raise RuntimeError("No usable token after reference credit check")
                _filelog(
                    f"[ReferenceImplementation] credit-check primary ok task={task.id} "
                    f"account={_session_account} token={_credit_info.get('token_fingerprint')} "
                    f"credits={_credit_info.get('credits')} tier={_credit_info.get('tier')}"
                )

                _initial_proxy = ""
                try:
                    from core.static_proxy_pool import get_fresh_proxy
                    _initial_proxy = get_fresh_proxy() or ""
                except Exception as _proxy_exc:
                    logger.warning(
                        "[ReferenceImplementation] proxy.txt initial load failed token=%s task=%s error=%s",
                        _token_sha8,
                        getattr(task, "id", ""),
                        _proxy_exc,
                    )
                _current_proxy_by_token = {_token: _initial_proxy}

                def _rotate_reference_proxy(old_token: str, reason: str) -> str | None:
                    token_key = hashlib.sha1(str(old_token or "").encode("utf-8", errors="ignore")).hexdigest()[:12]
                    current_proxy = (_current_proxy_by_token.get(old_token) or "").strip()
                    try:
                        from core.static_proxy_pool import get_fresh_proxy, mark_proxy_dead
                        if current_proxy:
                            mark_proxy_dead(current_proxy, ttl=120)
                        proxy = get_fresh_proxy(exclude={current_proxy} if current_proxy else set())
                    except Exception as exc:
                        logger.warning("[ReferenceImplementation] proxy_rotate_on_500 failed token=%s error=%s", token_key, exc)
                        return None
                    if proxy:
                        _current_proxy_by_token[old_token] = proxy
                        logger.warning(
                            "[ReferenceImplementation] proxy_rotate_on_500 rotated token=%s old_proxy=%s new_proxy=%s source=proxy.txt reason=%s",
                            token_key,
                            current_proxy or "direct",
                            proxy,
                            reason,
                        )
                        return proxy
                    logger.warning("[ReferenceImplementation] proxy_rotate_on_500 cooldown_or_empty token=%s old_proxy=%s", token_key, current_proxy or "direct")
                    return None
                _filelog(
                    f"[ReferenceImplementation] dispatch request task={task.id} "
                    f"account={_session_account} token={_token_sha8} project={_project_id} "
                    f"proxy={_initial_proxy or 'direct'} proxy_source=proxy.txt "
                    f"mode={getattr(task, 'mode', '') or getattr(task, 'action_type', '')} "
                    f"prompt_len={len(str(getattr(task, 'prompt', '') or ''))}"
                )

                _aspect_map = {
                    "IMAGE_ASPECT_RATIO_PORTRAIT": "9:16",
                    "IMAGE_ASPECT_RATIO_LANDSCAPE": "16:9",
                    "IMAGE_ASPECT_RATIO_SQUARE": "1:1",
                }
                _raw = task.raw_result if isinstance(task.raw_result, dict) else {}

                from web.models import ActionType
                _action_value = getattr(getattr(task, "action_type", None), "value", getattr(task, "action_type", None))
                _is_video = _action_value in ("TEXT_TO_VIDEO", "IMAGE_TO_VIDEO", "IMAGES_TO_VIDEO", "FRAMES_TO_VIDEO", "CREATE_VIDEO_I2V", "CREATE_VIDEO_START_END_IMAGE")
                _mode = "video" if _is_video else "image"
                _model = "VIDEO_I2V" if _mode == "video" else ("GEM_PIX_2" if task.model in ("NARWHAL", "GEMINI", "GEM_PIX_2") else task.model)
                _prompt = (task.prompts or [""])[0]
                _prompts = [_prompt for _ in range(max(1, int(getattr(task, "count", 1) or 1)))]


                _video_type = "single"
                if _action_value in ("IMAGE_TO_VIDEO", "CREATE_VIDEO_I2V"):
                    _video_type = "reference"
                elif _action_value in ("IMAGES_TO_VIDEO", "CREATE_VIDEO_START_END_IMAGE"):
                    _video_type = "start_end"
                elif _action_value == "FRAMES_TO_VIDEO":
                    _video_type = "frames"

                _thread_count = 1 if _mode == "video" else max(1, min(_configured_threads, 10))
                
                _image_refs = task.image_refs or []
                _end_image_refs = getattr(task, "end_image_refs", [])
                
                if not _image_refs:
                    if _raw.get("image_path"):
                        _image_refs = [_raw.get("image_path")]
                    elif _raw.get("images_b64"):
                        _paths = [img.get("path") for img in _raw.get("images_b64") if img.get("path")]
                        if _paths:
                            _image_refs = _paths
                
                if not _end_image_refs and _raw.get("end_image_path"):
                    _end_image_refs = [_raw.get("end_image_path")]

                if _mode == "video" and _video_type == "start_end" and len(_image_refs) > 1 and not _end_image_refs:
                    _end_image_refs = [_image_refs[-1]]
                    _image_refs = [_image_refs[0]]

                _runtime_session_id = str(getattr(task, "id", "") or uuid.uuid4().hex)
                _runtime_tokens = [_token]
                _runtime_proxies = [_initial_proxy] if _initial_proxy else []
                _runtime_token_project_map = {_token: _project_id}
                _runtime_primary_token = _token
                _runtime_primary_project_id = _project_id
                if not _picked_account_name:
                    try:
                        _active_accounts = [
                            account for account in store.list_veo_accounts()
                            if getattr(account, "is_active", False)
                            and (getattr(account, "cookie", None) or "").strip()
                        ]
                        _sessions_by_token = {}
                        _account_by_token = {}
                        for _account in _active_accounts:
                            _account_name = (getattr(_account, "name", "") or "").strip()
                            if not _account_name:
                                continue
                            try:
                                _candidate = account_session_api.issue_for_account_name(_account_name, user=_user)
                            except Exception as _multi_session_exc:
                                logger.warning(
                                    "[ReferenceImplementation] multi-token skip account=%s error=%s",
                                    _account_name,
                                    _multi_session_exc,
                                )
                                continue
                            _candidate_token = (_candidate.get("token") or "").strip()
                            _candidate_project = (_candidate.get("project_id") or "").strip()
                            if not _candidate_token or not _candidate_project:
                                continue
                            _candidate_proxy = ""
                            _credit_info = _reference_check_token_credit_local(_candidate_token, _candidate_proxy, _candidate_project)
                            if not _credit_info.get("ok"):
                                logger.warning(
                                    "[ReferenceImplementation] credit-check skip account=%s token=%s error=%s",
                                    _account_name,
                                    _credit_info.get("token_fingerprint"),
                                    _credit_info.get("error"),
                                )
                                _filelog(
                                    f"[ReferenceImplementation] credit-check skip account={_account_name} "
                                    f"token={_credit_info.get('token_fingerprint')} error={_credit_info.get('error')}"
                                )
                                continue
                            logger.info(
                                "[ReferenceImplementation] credit-check account=%s token=%s ok credits=%s tier=%s",
                                _account_name,
                                _credit_info.get("token_fingerprint"),
                                _credit_info.get("credits"),
                                _credit_info.get("tier"),
                            )
                            _filelog(
                                f"[ReferenceImplementation] credit-check account={_account_name} "
                                f"token={_credit_info.get('token_fingerprint')} ok "
                                f"credits={_credit_info.get('credits')} tier={_credit_info.get('tier')}"
                            )
                            _sessions_by_token[_candidate_token] = _candidate_project
                            _account_by_token[_candidate_token] = _candidate.get("account_name") or _account_name
                        if _sessions_by_token:
                            _runtime_tokens = list(_sessions_by_token.keys())
                            _runtime_token_project_map = dict(_sessions_by_token)
                            _runtime_primary_token = _runtime_tokens[0]
                            _runtime_primary_project_id = _runtime_token_project_map[_runtime_primary_token]
                            try:
                                from core.static_proxy_pool import get_all_proxies
                                _all_proxies = [str(p).strip() for p in (get_all_proxies() or []) if str(p).strip()]
                            except Exception as _all_proxy_exc:
                                logger.warning("[ReferenceImplementation] proxy.txt multi-load failed error=%s", _all_proxy_exc)
                                _all_proxies = []
                            _runtime_proxies = _all_proxies[:len(_runtime_tokens)]
                            _current_proxy_by_token = {
                                token_value: (_runtime_proxies[index] if index < len(_runtime_proxies) else "")
                                for index, token_value in enumerate(_runtime_tokens)
                            }
                            _filelog(
                                f"[ReferenceImplementation] multi-token dispatch tokens={len(_runtime_tokens)} "
                                f"proxies={len([p for p in _runtime_proxies if p])} "
                                "single_chrome_per_token=1 source=active_accounts"
                            )
                    except Exception as _multi_setup_exc:
                        logger.warning("[ReferenceImplementation] multi-token setup failed; fallback single token error=%s", _multi_setup_exc)

                _thread_count = max(
                    1,
                    min(
                        len(_prompts),
                        max(1, len(_runtime_tokens)) * 3,
                        max(1, _configured_threads),
                    ),
                )
                _filelog(
                    f"[{_action_value}][ReferenceImplementation] dispatch task={task.id} "
                    f"runtime_session={_runtime_session_id} mode={_mode} model={_model} "
                    f"video_type={_video_type} aspect={_aspect_map.get(task.screen_ratio, task.screen_ratio)} "
                    f"thread_count={_thread_count} max_attempts={_max_attempts} output_dir={_output_dir} "
                    f"tokens={len(_runtime_tokens)} refs={_image_refs} end_refs={_end_image_refs} prompts={len(_prompts)}"
                )
                task.status = TaskStatus.PROCESSING
                task.raw_result = {
                    **_raw,
                    "engine": "reference-implementation",
                    "account_name": _session.get("account_name"),
                    "project_id": _runtime_primary_project_id,
                }
                store.update_video_task(task)

                _filelog(
                    f"[{_action_value}][ReferenceImplementation] dispatch without global runtime lock task={task.id} "
                    f"mode={_mode} thread_count={_thread_count} tokens={len(_runtime_tokens)} "
                    "scheduler=token_gate single_chrome_per_token=1"
                )
                _results = run_reference_jobs(
                    token=_runtime_primary_token,
                    project_id=_runtime_primary_project_id,
                    prompts=_prompts,
                    mode=_mode,
                    model=_model,
                    aspect_ratio=_aspect_map.get(task.screen_ratio, task.screen_ratio),
                    thread_count=_thread_count,
                    output_dir=_output_dir,
                    max_attempts=_max_attempts,
                    reference_paths=_image_refs,
                    end_reference_paths=_end_image_refs,
                    video_type=_video_type,
                    image_resolution=_raw.get("upsample_resolution") or "1K",
                    output_prefix=task.id,
                    runtime_session_id=_runtime_session_id,
                    rotate_proxy_callback=_rotate_reference_proxy,
                    tokens=_runtime_tokens,
                    token_project_map=_runtime_token_project_map,
                    proxies=_runtime_proxies,
                )
                
                _completed_paths = [r.get("saved_path") for r in _results if r.get("status") == "completed" and r.get("saved_path")]
                if _completed_paths:
                    task.media_id = _completed_paths[-1]

                task.status = TaskStatus.COMPLETED if any(r.get("status") == "completed" for r in _results) else TaskStatus.FAILED
                task.raw_result = {
                    "engine": "reference-implementation",
                    "account_name": _session.get("account_name"),
                    "project_id": _project_id,
                    "results": _results,
                    "image_paths": _completed_paths if _mode == "image" else [],
                    "video_paths": _completed_paths if _mode == "video" else [],
                }
                _errors = [r.get("error") for r in _results if r.get("error")]
                if _errors and task.status == TaskStatus.FAILED:
                    task.error = _errors[0]
                store.update_video_task(task)
                return
            except Exception as _reference_dispatch_error:
                logger.exception(
                    "[CreateImage][ReferenceImplementation] dispatch failed: %s",
                    _reference_dispatch_error,
                )
                task.status = TaskStatus.FAILED
                task.error = f"Reference implementation dispatch failed: {_reference_dispatch_error}"
                task.raw_result = {"engine": "reference-implementation", "error": str(_reference_dispatch_error)}
                store.update_video_task(task)
                return

            # ── Timeout check ──
            if self._check_task_timeout(task):
                return
            _filelog(f"[CreateImage] START task={task.id} model={task.model}")
            from core.imagen_client import ImagenClient, ImagenPermissionError

            # ── EARLY EXIT: Kiểm tra task đã có ảnh trên đĩa chưa ──
            # Tránh tạo ảnh trùng khi task bị re-enqueue nhiều lần
            import glob as _glob
            _output_dir_check = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output"
            )
            _existing_files = _glob.glob(
                os.path.join(_output_dir_check, f"imagen_{task.id[:8]}_*.png")
            )
            if _existing_files:
                # Task ĐÃ CÓ ảnh trên đĩa → đánh dấu COMPLETED ngay, không chạy lại API
                _newest = sorted(_existing_files)[-1]
                task.media_id = _newest
                task.output_filename = os.path.basename(_newest)
                task.status = TaskStatus.COMPLETED
                task.completed_at = time.time()
                task.error = None
                if not isinstance(task.raw_result, dict):
                    task.raw_result = {}
                task.raw_result["image_paths"] = _existing_files
                task.raw_result["source"] = "imagen"
                store.update_video_task(task)
                _filelog(
                    f"[CreateImage] Task {task.id} EARLY-COMPLETE: "
                    f"đã có {len(_existing_files)} ảnh trên đĩa → skip API"
                )
                return

            # Set PROCESSING ngay khi bắt đầu chạy
            task.status = TaskStatus.PROCESSING
            store.update_video_task(task)

            # ─── VEO3 (Imagen) ───
            logger.info(
                f"[VEO3][START] Task {task.id[:8]}... prompt={((task.prompts[0]) if task.prompts else '')[:40]}"
            )



            # ── RULE NO-SWAP TOÀN PHẦN ──────────────────────────────────────
            # Theo yêu cầu user: KHÔNG swap account dưới mọi hình thức.
            # Outer loop là `while True` cùng account, chỉ thoát khi:
            #   (a) success, hoặc
            #   (b) `_check_task_timeout` cancel (600s ảnh / 1200s video), hoặc
            #   (c) lỗi content vĩnh viễn không retry được (vd 400 UNSAFE prompt).
            # Lỗi F5-recoverable (UNUSUAL_ACTIVITY 403, 429 too much, RESOURCE_
            # EXHAUSTED, 5xx) → trigger F5 tab labs.google + retry cùng account
            # (giống user F5 trên web).
            active_accounts = [a for a in store.list_veo_accounts() if a.is_active]
            pool_size = len(active_accounts)
            _filelog(
                f"[CreateImage] Task {task.id}: {pool_size} active accounts, "
                f"NO-SWAP mode (lock 1 account, F5+retry vô hạn)"
            )

            # ── GUARD: Nếu task đã COMPLETED hoặc FAILED từ lần chạy trước, KHÔNG chạy lại ──
            # FAILED guard chặn trường hợp task bị timeout-fail ở recovery, client submit
            # lại cùng task.id qua API → server phải từ chối thay vì chạy lại task đã chết.
            _db_task = store.get_video_task(task.id)
            if _db_task and _db_task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                logger.info(
                    f"[CreateImage] Task {task.id[:8]}... đã {_db_task.status.value} "
                    f"(từ lần chạy trước) → SKIP, không chạy lại."
                )
                return

            task.status = TaskStatus.PROCESSING
            store.update_video_task(task)

            prompt = task.prompts[0] if task.prompts else ""
            # Imagen hỗ trợ NARWHAL (cũ), IMAGEN_3_5, GEM_PIX_2
            # ÉP BUỘC dùng GEM_PIX_2 vì model này ổn định nhất và ít bị Google quét Unusual Activity
            model_key = "GEM_PIX_2"
            if model_key != task.model:
                logger.info(
                    f"[CreateImage] Task {task.id}: forcing model 'GEM_PIX_2' (requested: {task.model}) for stability"
                )

            # Tính aspect ratio 1 lần (đã thêm strip() để tránh khoảng trắng ẩn)
            _sr = (task.screen_ratio or "").upper().strip()
            if "PORTRAIT" in _sr or "9:16" in _sr:
                aspect_raw = "IMAGE_ASPECT_RATIO_PORTRAIT"
            elif "SQUARE" in _sr or "1:1" in _sr or "SQUARE" in _sr:
                aspect_raw = "IMAGE_ASPECT_RATIO_SQUARE"
            else:
                aspect_raw = "IMAGE_ASPECT_RATIO_LANDSCAPE"

            # Lấy upsample resolution từ raw_result (nếu có)
            upsample_res = None
            if isinstance(task.raw_result, dict):
                upsample_res = task.raw_result.get("upsample_resolution")
            # upsample_res sẽ là "2K" hoặc "4K" (nếu user chọn) hoặc None (không upscale)

            # tried_account_names giữ var để tương thích các nhánh code khác,
            # nhưng KHÔNG add vào (no-swap rule). Sticky account_locked giữ
            # account đã pick lần đầu — các iter sau buộc dùng cùng account.
            tried_account_names = set()
            last_403_error = None
            account_locked = None  # email account khoá cứng cho task này
            cookie_attempt = -1     # chỉ dùng để log "Attempt N"

            while True:
                cookie_attempt += 1
                # ── Timeout guard duy nhất (600s ảnh / 1200s video) ──
                if self._check_task_timeout(task):
                    return

                # ── Check: task có bị PAUSED giữa chừng không? ──
                _fresh_c = store.get_video_task(task.id)
                if _fresh_c and str(getattr(_fresh_c, 'status', '')).upper() == 'PAUSED':
                    logger.info(f"[CreateImage] ⏸️ Task {task.id} đã bị PAUSED → dừng retry")
                    return

                # Step 1: Pick cookie — LOCK account ở lần đầu, các lần sau
                # buộc dùng đúng account đó (no-swap).
                if account_locked and not task.picked_account_name:
                    task.picked_account_name = account_locked  # restore lock nếu bị reset
                task_client = self._resolve_client_for_task(
                    task, exclude_names=set()  # KHÔNG exclude → giữ cùng account
                )
                if not task_client:
                    # Account locked chưa available (busy / dead tạm thời) → đợi rồi retry.
                    # KHÔNG break (sẽ làm task FAIL); chỉ timeout 600s mới cancel.
                    _filelog(
                        f"[CreateImage] Task {task.id}: _resolve_client trả None "
                        f"(locked={account_locked}) → sleep 5s rồi retry"
                    )
                    time.sleep(5)
                    continue
                account_label = getattr(task_client, "_account_label", None) or task.picked_account_name or "task-cookie"
                # Lock account tại lần pick đầu tiên
                if not account_locked:
                    account_locked = account_label
                    task.picked_account_name = account_label
                    _filelog(
                        f"[CreateImage] Task {task.id}: LOCKED account={account_label} "
                        f"(no-swap, F5+retry vô hạn cho mọi lỗi)"
                    )

                # (403 backoff đã xóa — 403 = lỗi captcha worker, không phải account tạo)

                # ── Check daily quota exhaustion ──────────────────────────
                # NO-SWAP: vẫn dùng CÙNG account (đã lock), chỉ chờ qua cooldown.
                # F5 + sleep ngắn rồi retry — timeout 600s guard duy nhất.
                _quota_until = self._account_daily_quota_exhausted.get(account_label, 0)
                if _quota_until > time.time():
                    _remaining = int(_quota_until - time.time())
                    logger.warning(
                        f"[CreateImage] Account {account_label} DAILY QUOTA exhausted "
                        f"({_remaining}s remaining) → F5 + sleep 5s + retry SAME account (no-swap)"
                    )
                    self._trigger_f5(account_label, reason="daily_quota_cooldown")
                    continue
                elif _quota_until:
                    self._account_daily_quota_exhausted.pop(account_label, None)
                    logger.info(
                        f"[CreateImage] Account {account_label} daily quota backoff hết → dùng lại"
                    )
                # ─────────────────────────────────────────────────────────────

                # ── Check create_project blacklist ────────────────────────
                # NO-SWAP: lỗi tạo project có thể tạm thời, F5 reset state + retry
                # cùng account. Project blacklist đã được clear ở Step 3 khi
                # create_project sau đó OK.
                if account_label in self._account_project_blacklist:
                    logger.warning(
                        f"[CreateImage] Account {account_label} bị blacklist (create_project FAILED) "
                        f"→ F5 + retry SAME account (no-swap)"
                    )
                    self._trigger_f5(account_label, reason="project_blacklist")
                    # Clear blacklist để Step 3 thử tạo project lại
                    self._account_project_blacklist.discard(account_label)
                    continue
                # ──────────────────────────────────────────────────────────

                # Track timestamp (không enforce cooldown cứng nữa)

                # Rotating proxy goc (chua co session cu the) de tao pinned session
                _rotating_proxy = getattr(task_client, "_rotating_proxy", None)
                static_proxy = task_client.proxy  # proxy tinh cho API calls

                # ── Chọn proxy cho ImagenClient ─────────────────────────────────────────
                # Dùng static proxy trực tiếp (rotate_proxy đã bị loại).
                session_proxy = static_proxy

                # Step 2: Tạo ImagenClient
                api_proxy = session_proxy

                from core.hybrid_imagen_client import HybridImagenClient
                img_client = HybridImagenClient(
                    cookie=task_client.cookie, proxy=api_proxy,
                    access_token=task_client.access_token,
                    account_email=account_label,
                )
                logger.info(
                    f"[CreateImage] Attempt {cookie_attempt+1} (no-swap, locked={account_label}) "
                    f"proxy={'YES: '+api_proxy[:30] if api_proxy else 'NO PROXY (extension)'} "
                    f"token={'OK' if img_client.access_token else 'FAIL'} "
                    f"browser={'ON' if img_client._browser_available() else 'OFF'}"
                )
                # NO-SWAP: bridge ON có thể tự lấy token trong tab → continue.
                # Token=FAIL + bridge=OFF: KHÔNG swap. Reload cookie từ DB +
                # F5 + retry CÙNG account vô hạn (timeout 600s guard).
                if not img_client.access_token and not img_client._browser_available():
                    logger.warning(
                        f"[CreateImage] Token FAIL + bridge OFF trên {account_label} "
                        f"→ reload cookie + F5 + retry SAME account (no-swap)"
                    )
                    # Reload cookie từ DB phòng trường hợp extension đã push token mới
                    try:
                        _fresh_acc = store.get_veo_account_by_name(account_label)
                        if _fresh_acc and _fresh_acc.cookie:
                            _proxy_for_reload = self._resolve_proxy_for_account(_fresh_acc)
                            from core.hybrid_veo_client import HybridVeoClient as _HVC
                            _new_task_client = _HVC(
                                _fresh_acc.cookie, proxy=_proxy_for_reload,
                                account_email=_fresh_acc.name,
                            )
                            task_client = _new_task_client
                            setattr(task_client, "_account_label", account_label)
                    except Exception as _re:
                        logger.warning(f"[CreateImage] Token-fail reload cookie error: {_re}")
                    self._trigger_f5(account_label, reason="token_fail_bridge_off")
                    continue

                # Step 3: Lấy/Tạo GL project (cache per account + UI project, max 700 ảnh/project)
                # Mỗi UI project trên mỗi account có GL project riêng.
                # Khi GL project đạt 700 ảnh → tự tạo project mới.
                # Double-checked locking: chỉ 1 thread tạo project, các thread khác đợi.
                _cache_key = (account_label, task.project_id)
                gl_project = task.project_id  # fallback
                _need_new_project = False

                _cached_entry = self._gl_project_cache.get(_cache_key)
                if _cached_entry:
                    if _cached_entry["count"] < self.MAX_IMAGES_PER_GL_PROJECT:
                        gl_project = _cached_entry["id"]
                        logger.info(
                            f"[CreateImage] Reusing GL project: {gl_project[:20]}... "
                            f"(count={_cached_entry['count']}/{self.MAX_IMAGES_PER_GL_PROJECT})"
                        )
                    else:
                        _need_new_project = True
                        logger.info(
                            f"[CreateImage] GL project đầy ({_cached_entry['count']}>={self.MAX_IMAGES_PER_GL_PROJECT}) → tạo mới"
                        )
                else:
                    _need_new_project = True

                if _need_new_project:
                    # Lấy lock riêng cho (account, ui_project)
                    with self._project_locks_meta:
                        if _cache_key not in self._project_locks:
                            self._project_locks[_cache_key] = threading.Lock()
                    _proj_lock = self._project_locks[_cache_key]

                    with _proj_lock:  # Chỉ 1 thread vào đây cùng lúc
                        # Double-check sau khi lấy được lock
                        _cached_entry = self._gl_project_cache.get(_cache_key)
                        if (
                            _cached_entry
                            and _cached_entry["count"] < self.MAX_IMAGES_PER_GL_PROJECT
                        ):
                            gl_project = _cached_entry["id"]
                            logger.info(
                                f"[CreateImage] Reusing GL project (locked): {gl_project[:20]}... "
                                f"(count={_cached_entry['count']}/{self.MAX_IMAGES_PER_GL_PROJECT})"
                            )
                        else:
                            try:
                                from core.project import create_project, search_user_projects

                                # ── Ưu tiên lấy project mới nhất đã có ──
                                _proj = None
                                try:
                                    _existing = search_user_projects(
                                        cookie=task_client.cookie,
                                        access_token=task_client.access_token,
                                        page_size=1,
                                        timeout=8,
                                        proxy=static_proxy,
                                    )
                                    if _existing and len(_existing) > 0:
                                        _proj = _existing[0].get("projectId")
                                        if _proj:
                                            logger.info(
                                                f"[CreateImage] Reusing existing GL project: {_proj[:20]}... "
                                                f"(title={_existing[0].get('projectTitle', '?')})"
                                            )
                                except Exception as _se:
                                    logger.warning(f"[CreateImage] search_user_projects failed: {_se}")

                                # ── Fallback: tạo mới nếu không tìm được ──
                                if not _proj:
                                    _proj = create_project(
                                        f"API-{task.project_id[:8]}",
                                        tool_name="PINHOLE",
                                        cookie=task_client.cookie,
                                        access_token=task_client.access_token,
                                        browser_headers=getattr(
                                            task_client, "base_headers", None
                                        ),
                                        proxy=static_proxy,
                                    )
                                    if _proj:
                                        logger.info(
                                            f"[CreateImage] Created new GL project: {_proj[:20]}... "
                                            f"(ui_project={task.project_id[:8]})"
                                        )

                                if _proj:
                                    gl_project = _proj
                                    self._account_project_blacklist.discard(account_label)
                                    self._gl_project_cache[_cache_key] = {
                                        "id": _proj,
                                        "count": 0,
                                    }
                                else:
                                    # ── AUTO-ROTATE PROXY ON CREATE_PROJECT FAILURE ──
                                    # Cố gắng lấy session mới trước khi rotate proxy (user suggested)
                                    try:
                                        task_client.get_session_token()
                                    except: pass

                                    new_ip = self._rotate_kiotproxy_key_for_account(account_label)
                                    # Fallback: static proxy pool
                                    if not new_ip:
                                        try:
                                            from core.static_proxy_pool import get_random_proxy
                                            new_ip = get_random_proxy()
                                        except Exception:
                                            pass
                                    if new_ip:
                                        logger.warning(
                                            f"[CreateImage] create_project FAILED trên {account_label} "
                                            f"→ Token refreshed & proxy rotated -> {new_ip[:30]}, retry same account"
                                        )
                                        time.sleep(1)  # TCP reconnect buffer
                                        continue # Thử lại cùng account với IP mới và Token mới
                                    
                                    # NO-SWAP: rotate proxy fail → F5 + retry SAME account.
                                    # KHÔNG ban account, KHÔNG swap. Timeout 600s guard.
                                    logger.warning(
                                        f"[CreateImage] create_project FAILED & rotate proxy fail "
                                        f"→ F5 + sleep + retry SAME account {account_label} (no-swap)"
                                    )
                                    self._trigger_f5(account_label, reason="create_project_rotate_fail")
                                    continue
                            except Exception as e:
                                logger.warning(
                                    f"[CreateImage] create_project exception (non-fatal): {e}"
                                )

                # Step 4: Upload ảnh tham khảo nếu có (upload trước, rồi mới xin captcha)
                # RULE NO-SWAP: pool proxy exhausted hoặc lỗi non-proxy → KHÔNG FAIL,
                # KHÔNG swap. Reload cookie + xoay proxy + retry SAME account vô hạn.
                # Chỉ timeout 600s mới cancel.
                media_ids = []
                upload_pool_exhausted = False
                if task.image_refs:
                    # Cache mediaId per ref index — upload OK 1 lần là dùng được
                    # cho mọi cycle/retry sau (KHÔNG re-upload, KHÔNG swap account).
                    _ref_cached_mid = [None] * len(task.image_refs)

                    def _single_upload(ref_idx_item):
                        nonlocal upload_pool_exhausted
                        ref_idx, ref_item = ref_idx_item
                        # Đã upload OK trước đó → reuse
                        if _ref_cached_mid[ref_idx]:
                            return _ref_cached_mid[ref_idx]
                        try:
                            if isinstance(ref_item, str) and ref_item.startswith("projects/"):
                                _ref_cached_mid[ref_idx] = ref_item
                                return ref_item
                            _mid = None
                            _err = None
                            if isinstance(ref_item, dict):
                                p_data = ref_item.get("path", "")
                                b_data = ref_item.get("b64", "")
                                if p_data and os.path.exists(p_data):
                                    _mid, _err = self._upload_with_proxy_retry(
                                        img_client,
                                        img_client.upload_image_from_path,
                                        p_data,
                                        project_id=gl_project,
                                        flow="CreateImage-ref-path",
                                        task_id=task.id,
                                    )
                                elif b_data:
                                    _mid, _err = self._upload_with_proxy_retry(
                                        img_client,
                                        img_client.upload_user_image,
                                        b_data,
                                        mime=ref_item.get("mime", "image/jpeg"),
                                        file_name=ref_item.get("name", "upload.jpg"),
                                        project_id=gl_project,
                                        flow="CreateImage-ref-b64",
                                        task_id=task.id,
                                    )
                            elif isinstance(ref_item, str) and os.path.exists(ref_item):
                                _mid, _err = self._upload_with_proxy_retry(
                                    img_client,
                                    img_client.upload_image_from_path,
                                    ref_item,
                                    project_id=gl_project,
                                    flow="CreateImage-ref-path",
                                    task_id=task.id,
                                )
                            if _err == "proxy_exhausted":
                                upload_pool_exhausted = True
                            if _mid:
                                _ref_cached_mid[ref_idx] = _mid
                            return _mid
                        except Exception: pass
                        return None

                    # Retry vô hạn trên CÙNG account cho tới khi tất cả ảnh upload OK
                    # hoặc timeout 600s cancel. Ref nào đã upload OK sẽ KHÔNG re-upload.
                    _upload_cycle = 0
                    while True:
                        _upload_cycle += 1
                        # Timeout guard
                        if self._check_task_timeout(task):
                            logger.warning(
                                f"[CreateImage] Task {task.id}: upload TIMEOUT → cancelled"
                            )
                            self._release_cookie(account_label)
                            self._clear_activity(account_label)
                            return
                        upload_pool_exhausted = False
                        _items = list(enumerate(task.image_refs))
                        with _futures.ThreadPoolExecutor(max_workers=10) as executor:
                            list(executor.map(_single_upload, _items))
                        media_ids = [m for m in _ref_cached_mid if m]
                        if len(media_ids) == len(task.image_refs):
                            break  # Tất cả ảnh upload OK (có thể từ nhiều cycle khác nhau)
                        if not media_ids:
                            logger.warning(
                                f"[CreateImage] Upload cycle {_upload_cycle}: TOÀN BỘ ref fail "
                                f"(pool_exhausted={upload_pool_exhausted}) → reload cookie + xoay proxy "
                                f"→ retry SAME account {account_label} (rule no-swap)"
                            )
                        else:
                            logger.warning(
                                f"[CreateImage] Upload cycle {_upload_cycle}: {len(media_ids)}/"
                                f"{len(task.image_refs)} ref OK → retry phần còn lại trên SAME account"
                            )

                        # Reload cookie từ DB
                        try:
                            _fresh_acc = store.get_veo_account_by_name(account_label)
                            if _fresh_acc and _fresh_acc.cookie:
                                _proxy_for_reload = self._resolve_proxy_for_account(_fresh_acc)
                                from core.hybrid_veo_client import HybridVeoClient as _HVC
                                _new_task_client = _HVC(
                                    _fresh_acc.cookie, proxy=_proxy_for_reload,
                                    account_email=_fresh_acc.name,
                                )
                                if _new_task_client.access_token:
                                    task_client = _new_task_client
                                    setattr(task_client, "_account_label", account_label)
                                    from core.hybrid_imagen_client import HybridImagenClient as _HIC
                                    img_client = _HIC(
                                        cookie=_fresh_acc.cookie,
                                        proxy=_proxy_for_reload,
                                        access_token=_new_task_client.access_token,
                                        account_email=account_label,
                                    )
                        except Exception as _re:
                            logger.warning(f"[CreateImage] Upload reload cookie error: {_re}")

                        # Xoay proxy từ pool: KiotProxy → static fallback
                        # Mark proxy hiện tại dead + lấy fresh từ static pool.
                        # KHÔNG rotate KiotProxy (gây ban IP).
                        _old_proxy = getattr(img_client, "proxy", None) or getattr(task_client, "proxy", None)
                        new_ip = None
                        try:
                            from core.static_proxy_pool import get_fresh_proxy as _gfp, mark_proxy_dead as _mpd
                            if _old_proxy:
                                _mpd(_old_proxy)
                            new_ip = _gfp(exclude={_old_proxy} if _old_proxy else None)
                        except Exception:
                            new_ip = None
                        if new_ip:
                            try:
                                img_client.proxy = new_ip
                            except Exception:
                                pass
                            try:
                                task_client.proxy = new_ip
                            except Exception:
                                pass
                        time.sleep(3)

                # (Bỏ sleep 10s sau upload ref — _account_api_gate 5s đã đủ chống burst)

                # Step 5: Giải Captcha — chỉ xin token khi sắp gọi Generate
                captcha = ""
                try:
                    setattr(task_client, "_account_label", account_label)
                    captcha = self._solve_captcha_smart(
                        task_client, action="IMAGE_GENERATION"
                    )
                except ImportError:
                    logger.warning("[ImageCaptcha] captcha_solver not available")
                except Exception as cap_e:
                    logger.warning(f"[ImageCaptcha] Unexpected error: {cap_e}")

                # ── Log để trace media_ids trước khi generate ──
                if media_ids:
                    logger.info(
                        f"[CreateImage] ✅ Sẽ generate VỚI {len(media_ids)} reference image(s) "
                        f"→ gl_project={gl_project[:30]} | "
                        f"media_ids={[m[:40] for m in media_ids]}"
                    )
                else:
                    logger.info(
                        f"[CreateImage] Generate KHÔNG có reference image (text-only) "
                        f"→ gl_project={gl_project[:30]}"
                    )

                # Step 6: Goi API Generate — Banana lane serialisation (doc §05)
                result = None
                last_error = ""
                used_pw_fallback = False
                _captcha_403_retries = 0  # Chỉ để log — không giới hạn (timeout là guard duy nhất)
                MAX_API_RETRIES = 8  # chỉ áp cho lỗi non-403 (403 không tăng api_retry)
                api_retry = 0
                _lane = self._lane_manager.get_lane(account_label)  # Banana §05: serialised create per account
                while api_retry < MAX_API_RETRIES:
                    # ── TIMEOUT CHECK: fail task nếu đã chạy quá lâu (600s ảnh / 1200s video) ──
                    if self._check_task_timeout(task):
                        logger.warning(
                            f"[CreateImage] Task {task.id}: TIMEOUT → auto-cancelled "
                            f"(403-burn={_captcha_403_retries})"
                        )
                        self._release_cookie(account_label)
                        self._clear_activity(account_label)
                        return

                    # ── ZOMBIE CHECK: tránh chạy tiếp nếu task đã DONE bởi instance khác ──
                    try:
                        _fresh = store.get_video_task(task.id)
                        if _fresh and _fresh.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                            logger.warning(
                                f"[CreateImage] Task {task.id} đã {_fresh.status.value} bởi instance khác → dừng zombie"
                            )
                            _filelog(f"[CreateImage] ⚠️ Zombie detected: task {task.id} already {_fresh.status.value}")
                            return
                    except Exception:
                        pass
                    

                    try:
                        # ── Lấy CAPTCHA TOKEN MỚI cho mỗi retry ──
                        # Token captcha chỉ xài được 1 lần duy nhất.
                        # Nếu retry > 0, token cũ đã bị Google tiêu hủy → phải lấy mới.
                        _should_increment = True  # Mặc định: tính lượt retry này
                        if api_retry > 0:
                            logger.info(
                                f"[CreateImage] retry={api_retry} → LẤY CAPTCHA TOKEN MỚI (token cũ đã bị tiêu hủy)"
                            )
                            try:
                                setattr(task_client, "_account_label", account_label)
                                new_captcha = self._solve_captcha_smart(
                                    task_client, action="IMAGE_GENERATION"
                                )
                                if new_captcha:
                                    captcha = new_captcha
                                    logger.info(
                                        f"[CreateImage] ✅ Fresh token OK (len={len(captcha)})"
                                    )
                                else:
                                    logger.warning(
                                        "[CreateImage] ❌ Không lấy được token mới → clear token (không dùng lại token cũ)"
                                    )
                                    captcha = ""  # Force empty → guard bên dưới sẽ skip API call
                            except Exception as e:
                                logger.warning(
                                    f"[CreateImage] Token refresh error: {e}"
                                )

                        logger.info(
                            f"[CreateImage] Generating (retry={api_retry}) -- account={account_label}"
                            f" -- prompt: {prompt[:60]}"
                        )
                        # ── Guard: không gửi request nếu captcha token rỗng ──
                        if not captcha:
                            logger.warning(
                                f"[CreateImage] Captcha token rỗng → skip API call, retry captcha"
                            )
                            continue
                        # Cập nhật cooldown timestamp trước khi gọi API
                        self._last_image_request[account_label] = time.time()
                        # Main API flow: do not serialize create-image per account.
                        # Worker/thread count and token distribution control parallelism.
                        self._set_activity(account_label, "CALLING_API", task.id)
                        try:
                            result = img_client.generate_image(
                                row=0,
                                prompt=prompt,
                                project_id=gl_project,
                                captcha_token=captcha,
                                model=model_key,
                                image_media_ids=media_ids,
                                count=getattr(task, "count", 1) or 1,
                                aspect=aspect_raw,
                            )
                        except Exception as img_err:
                            img_client._last_error_detail = str(img_err)
                            result = None

                        if result:
                            # Báo pool: token này PASS → có thể dùng lại 1 lần
                            try:
                                from core.captcha_pool import get_pool as _get_pool
                                _pool_entry = getattr(task_client, "_last_pool_entry", None)
                                if _pool_entry:
                                    _get_pool().report_result(_pool_entry, passed=True)
                                    # Ghi nhận captcha PASS
                                    self._record_captcha_stat(account_label, _pool_entry.source, "pass")
                            except Exception:
                                pass
                            break  # Success

                        api_detail = (
                            getattr(img_client, "_last_error_detail", None)
                            or "no detail"
                        )
                        last_error = f"[Attempt {cookie_attempt+1}] Image generate failed (retry #{self._unusual_activity_counts.get(account_label, 0)} on {account_label}). {api_detail}"
                        logger.warning(f"[CreateImage] Task {task.id}: {last_error}")

                        # ── Banana §06: Centralized error handling ──
                        _err_rv = self._handle_api_error_banana(
                            api_detail=api_detail,
                            account_label=account_label,
                            flow="CreateImage",
                            task_client=task_client,
                            img_client=img_client,
                            task=task,
                            captcha_action="IMAGE_GENERATION",
                        )

                        # ── Apply error handler results ──
                        if _err_rv["should_fail_task"]:
                            task.status = TaskStatus.FAILED
                            task.error = _err_rv["fail_error"]
                            store.update_video_task(task)
                            return

                        if _err_rv["new_task_client"]:
                            task_client = _err_rv["new_task_client"]
                        if _err_rv["new_img_client"]:
                            img_client = _err_rv["new_img_client"]
                        if _err_rv["new_captcha"]:
                            captcha = _err_rv["new_captcha"]

                        _should_increment = _err_rv["should_increment"]
                        if _err_rv["action"] == ErrorAction.F5_RETRY:
                            _captcha_403_retries += 1
                        continue

                    except Exception as e:
                        last_error = str(e)
                        break
                    finally:
                        if _should_increment:
                            api_retry += 1

                if not result:
                    # NO-SWAP: API generate exhaust local retries (api_retry hit
                    # MAX_API_RETRIES) → KHÔNG FAIL, KHÔNG swap. F5 nếu lỗi
                    # cuối F5-recoverable + continue outer loop để retry cùng account.
                    # Banana §06: centralized error classification for exhaust check
                    if is_f5_recoverable(last_error):
                        self._trigger_f5(account_label, reason="api_generate_exhaust_f5able")
                    else:
                        logger.warning(
                            f"[CreateImage] API generate exhaust trên {account_label} "
                            f"(non-F5-able err) → sleep 5s + retry SAME account"
                        )
                        time.sleep(5)
                    continue

                try:
                    # Generate thanh cong -- loop qua TAT CA anh tra ve (co the nhieu hon 1)
                    saved_paths = []
                    import time as _time_mod
                    # Track có tối thiểu 1 ảnh gọi upsample API trong task này
                    # → dùng để mark cooldown 15s cho account thay vì 10s mặc định.
                    _task_attempted_upsample = False

                    for img_idx, img_info in enumerate(result):
                        media_id_raw = img_info.get("media_id", "")
                        fife_url = img_info.get("fife_url", "")

                        logger.info(
                            f"[CreateImage] Processing image {img_idx+1}/{len(result)} "
                            f"media_id={media_id_raw[:30]}..."
                        )

                        # Step 7a: Upsample tung anh (giai lai captcha rieng cho tung anh)
                        final_bytes = None
                        _upsample_succeeded = False  # Track xem upsample thực sự thành công chưa
                        if upsample_res and media_id_raw:
                            _task_attempted_upsample = True
                            try:
                                logger.info(f"[CreateImage] Chờ 5s trước khi upsample...")
                                _time_mod.sleep(5)

                                logger.info(
                                    f"[CreateImage] Upsampling img {img_idx+1} @ {upsample_res} "
                                    f"| media_id={media_id_raw[:60]}"
                                )
                                up_captcha = ""
                                try:
                                    # Dùng _solve_captcha_smart (qua Chrome Extension HTTP server)
                                    up_captcha = self._solve_captcha_smart(
                                        task_client, action="IMAGE_GENERATION"
                                    ) or ""
                                except Exception as cap_e:
                                    logger.warning(
                                        f"[CreateImage] Upsample captcha [{img_idx}] failed: {cap_e}"
                                    )
                                # Đã thêm delay cứng 15s tránh limit Google

                                # Cập nhật cooldown timestamp trước khi gọi upsample API
                                self._last_image_request[account_label] = time.time()
                                # Non-sequential image flow: upsample uses configured workers/tokens; no per-account create gate.

                                # ── Upsample qua Proxy (liên tục trong vòng 2 phút) ───────────
                                final_bytes = None
                                _upsample_start = time.time()
                                _UPSAMPLE_TIMEOUT = 120  # 2 phút window giống generate
                                _up_attempt = 0

                                while time.time() - _upsample_start <= _UPSAMPLE_TIMEOUT:
                                    _up_attempt += 1
                                    
                                    # Đã gỡ bỏ logic đổi Static Proxy ở đây để tránh lỗi 403 UNUSUAL_ACTIVITY.
                                    # Tool sẽ tiếp tục sử dụng đúng IP/Connection đã dùng ở bước Generate.

                                    # ── ZOMBIE CHECK (tương tự generate) ──
                                    try:
                                        _fresh = store.get_video_task(task.id)
                                        if _fresh and _fresh.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                                            logger.warning(f"[CreateImage] Task {task.id} đã {_fresh.status.value} → dừng zombie Upsample")
                                            break
                                    except Exception: pass

                                    # Cập nhật cooldown timestamp trước khi gọi API upsample
                                    self._last_image_request[account_label] = time.time()
                                    # Non-sequential image flow: retry attempts are controlled by worker threads, not account gates.

                                    try:
                                        final_bytes = img_client.upsample_image(
                                            row=img_idx,
                                            media_id=media_id_raw,
                                            resolution=upsample_res,
                                            project_id=gl_project,
                                            captcha_token=up_captcha,
                                        )
                                    except Exception as _up_ex:
                                        img_client._last_error_detail = str(_up_ex)
                                        final_bytes = None

                                    if final_bytes:
                                        # Thành công
                                        try:
                                            from core.captcha_pool import get_pool as _get_pool
                                            _pool_entry = getattr(task_client, "_last_pool_entry", None)
                                            if _pool_entry:
                                                _get_pool().report_result(_pool_entry, passed=True)
                                                self._record_captcha_stat(account_label, _pool_entry.source, "pass")
                                        except Exception: pass

                                        _upsample_succeeded = True
                                        logger.info(f"[CreateImage] ✅ Upsample [{img_idx}] → {upsample_res} thành công ({len(final_bytes)} bytes).")
                                        break

                                    # Phân tích lỗi
                                    _up_err_detail = getattr(img_client, "_last_error_detail", None) or "no detail"
                                    logger.warning(f"[CreateImage] Upsample attempt {_up_attempt} failed: {_up_err_detail}")

                                    # Báo token fail cho pool
                                    try:
                                        from core.captcha_pool import get_pool as _get_pool
                                        _pool_entry = getattr(task_client, "_last_pool_entry", None)
                                        if _pool_entry:
                                            _get_pool().report_result(_pool_entry, passed=False)
                                            self._record_captcha_stat(account_label, _pool_entry.source, "fail")
                                    except Exception: pass

                                    # ── XỬ LÝ CÁC LOẠI LỖI ──
                                    # NO-SWAP: TOO_MUCH_TRAFFIC trong upsample → F5 + retry,
                                    # KHÔNG ban account.
                                    if "TOO_MUCH_TRAFFIC" in _up_err_detail:
                                        logger.warning(
                                            f"[CreateImage] Upsample TOO_MUCH_TRAFFIC trên {account_label} "
                                            f"→ F5 + retry SAME account (no-swap, no-ban)"
                                        )
                                        self._trigger_f5(account_label, reason="upsample_too_much_traffic")
                                    elif "UNUSUAL_ACTIVITY" in _up_err_detail:
                                        self._handle_unusual_activity(account_label, flow="Upsample")
                                        _ua_proxy = self._last_unusual_rotated_proxy.pop(account_label, None)
                                        if _ua_proxy:
                                            img_client.proxy = _ua_proxy
                                        # Retry với captcha mới
                                    elif "PROXY_DEAD" in _up_err_detail or any(x in _up_err_detail for x in ("IP_FILTER", "IP_INPUT", "10061", "refused", "ProxyError")):
                                        # ProxyError -> xin phép KiotProxy rotate, nhưng pool sẽ tự check cooldown 2 phút
                                        # Sau khi xin phép thì tiếp tục dùng account, k swap account
                                        new_ip = self._record_ip_error(account_label)
                                        if new_ip:
                                            task_client.proxy = new_ip
                                            logger.warning(f"[CreateImage] Lỗi Proxy Upsample → Đã đổi sang IP mới ({new_ip[:40]}), retry...")
                                        else:
                                            logger.warning(f"[CreateImage] Lỗi Proxy Upsample → Đang trong cooldown, retry với IP hiện tại sau 1s")
                                        time.sleep(1)
                                    else:
                                        # Lỗi mạng generic, HTTP 500, etc.
                                        logger.warning(f"[CreateImage] Lỗi generic Upsample → retry sau 1s (giữ account)")
                                        time.sleep(1)

                                    if time.time() - _upsample_start > _UPSAMPLE_TIMEOUT:
                                        logger.warning(f"[CreateImage] Upsample timeout ({_UPSAMPLE_TIMEOUT}s) → dừng")
                                        break

                                    # Lấy captcha mới cho lần retry
                                    try:
                                        logger.info(f"[CreateImage] Upsample thất bại, đang lấy captcha mới để retry...")
                                        _new_cap = self._solve_captcha_smart(task_client, action="IMAGE_GENERATION")
                                        if _new_cap:
                                            up_captcha = _new_cap
                                    except Exception: pass


                                if not final_bytes:
                                    _up_err_detail = getattr(
                                        img_client, "_last_error_detail", None
                                    ) or "unknown (no detail from client)"
                                    _up_diag = getattr(
                                        img_client, "_last_upsample_detail", None
                                    ) or {}

                                    # ── FALLBACK: Thử NanoAI upscale ──
                                    logger.info(
                                        f"[CreateImage] ❌ Google upsample [{img_idx}] thất bại "
                                        f"→ thử NanoAI fallback... (res={upsample_res})"
                                    )
                                    _nano_bytes = self._call_nanoai_upscale(
                                        access_token=img_client.access_token,
                                        media_id=media_id_raw,
                                        project_id=gl_project,
                                        resolution=upsample_res,
                                    )

                                    if _nano_bytes:
                                        final_bytes = _nano_bytes
                                        _upsample_succeeded = True
                                        logger.info(
                                            f"[CreateImage] ✅ NanoAI upscale [{img_idx}] → {upsample_res} "
                                            f"thành công ({len(_nano_bytes)} bytes)"
                                        )
                                    else:
                                        # NanoAI cũng thất bại → fallback ảnh gốc
                                        logger.warning(
                                            f"[CreateImage] ❌ Upsample [{img_idx}] THẤT BẠI (Google + NanoAI) "
                                            f"→ fallback ảnh gốc. Lý do Google: {_up_err_detail} | "
                                            f"diag={_up_diag}"
                                        )
                                        if not isinstance(task.raw_result, dict):
                                            task.raw_result = {}
                                        _errs = task.raw_result.get("upsample_errors") or []
                                        _errs.append({
                                            "img_idx": img_idx,
                                            "resolution": upsample_res,
                                            "error": str(_up_err_detail)[:400],
                                            "diag": _up_diag,
                                            "nanoai_fallback": "failed",
                                        })
                                        task.raw_result["upsample_errors"] = _errs
                            except Exception as up_e:
                                logger.warning(
                                    f"[CreateImage] Upsample [{img_idx}] exception: {up_e}"
                                )
                                if not isinstance(task.raw_result, dict):
                                    task.raw_result = {}
                                _errs = task.raw_result.get("upsample_errors") or []
                                _errs.append({
                                    "img_idx": img_idx,
                                    "resolution": upsample_res,
                                    "error": f"exception: {up_e}"[:400],
                                })
                                task.raw_result["upsample_errors"] = _errs
                        elif upsample_res and not media_id_raw:
                            logger.warning(
                                f"[CreateImage] ⚠️ upsample_res={upsample_res} nhưng media_id_raw "
                                f"rỗng → KHÔNG THỂ upsample (generate response thiếu name/media_id)"
                            )
                            if not isinstance(task.raw_result, dict):
                                task.raw_result = {}
                            _errs = task.raw_result.get("upsample_errors") or []
                            _errs.append({
                                "img_idx": img_idx,
                                "resolution": upsample_res,
                                "error": "media_id_raw rỗng — generate response thiếu field 'name'",
                            })
                            task.raw_result["upsample_errors"] = _errs

                        # Step 7b: Fallback -- fife CDN (Không dùng Proxy)
                        if not final_bytes and fife_url:
                            try:
                                logger.info(
                                    f"[CreateImage] Downloading [{img_idx}] via fife CDN (Direct)..."
                                )
                                # Backup proxy
                                _orig_px = img_client.proxy
                                img_client.proxy = None
                                final_bytes = img_client.download_from_fife(
                                    row=img_idx, fife_url=fife_url
                                )
                                img_client.proxy = _orig_px # Restore
                                if final_bytes:
                                    logger.info(
                                        f"[CreateImage] Downloaded [{img_idx}] via fife CDN ({len(final_bytes)} bytes)"
                                    )
                            except Exception as fife_e:
                                logger.warning(
                                    f"[CreateImage] Fife CDN download [{img_idx}] failed: {fife_e}"
                                )

                        # Step 7c: Fallback -- tai anh goc qua media API (Không dùng Proxy)
                        if not final_bytes and media_id_raw:
                            try:
                                _orig_px = img_client.proxy
                                img_client.proxy = None
                                final_bytes = img_client.download_image(
                                    row=img_idx, media_id=media_id_raw
                                )
                                img_client.proxy = _orig_px # Restore
                                if final_bytes:
                                    logger.info(
                                        f"[CreateImage] Downloaded [{img_idx}] via media API ({len(final_bytes)} bytes)"
                                    )
                            except Exception as dl_e:
                                logger.warning(
                                    f"[CreateImage] Media API download [{img_idx}] failed: {dl_e}"
                                )


                        # Step 8: Luu file với extension auto-detect từ magic bytes
                        # Google upsample API trả JPEG (/9j/...), generate có thể JPEG/PNG/WEBP
                        if final_bytes:
                            _output_dir = os.path.join(
                                os.path.dirname(
                                    os.path.dirname(os.path.abspath(__file__))
                                ),
                                "output",
                            )
                            os.makedirs(_output_dir, exist_ok=True)
                            # Detect format từ magic bytes → extension đúng
                            _ext = ".png"  # safe default
                            _fmt_detected = "unknown"
                            if len(final_bytes) >= 4:
                                _head = final_bytes[:4]
                                if _head[:3] == b"\xff\xd8\xff":
                                    _ext = ".jpg"
                                    _fmt_detected = "JPEG"
                                elif _head[:4] == b"\x89PNG":
                                    _ext = ".png"
                                    _fmt_detected = "PNG"
                                elif _head[:4] == b"RIFF":
                                    _ext = ".webp"
                                    _fmt_detected = "WEBP"
                            # Tên file phản ánh resolution THỰC TẾ (không phải requested)
                            _res_tag = upsample_res if _upsample_succeeded else "orig"
                            _ts = int(_time_mod.time())
                            _fname = (
                                f"imagen_{task.id[:8]}_{img_idx+1}_{_res_tag}_{_ts}{_ext}"
                            )
                            _fpath = os.path.join(_output_dir, _fname)
                            with open(_fpath, "wb") as _f:
                                _f.write(final_bytes)
                            saved_paths.append(_fpath)
                            logger.info(
                                f"[CreateImage] Saved img {img_idx+1} → {_fpath} "
                                f"({len(final_bytes)} bytes, format={_fmt_detected}, ext={_ext}) "
                                f"actual_res={'✅ ' + upsample_res if _upsample_succeeded else '❌ original'}"
                            )
                        else:
                            # Khong co bytes: dung fife_url lam fallback
                            if fife_url:
                                saved_paths.append(fife_url)
                                logger.info(
                                    f"[CreateImage] Using fife_url for img {img_idx+1}"
                                )

                        # Xử lý ảnh tiếp theo ngay lập tức

                    # Luu ket qua vao task
                    if saved_paths:
                        task.media_id = saved_paths[0]  # path dau tien lam chinh
                        task.output_filename = (
                            os.path.basename(saved_paths[0])
                            if os.path.exists(saved_paths[0])
                            else ""
                        )
                        # Luu danh sach tat ca paths vao raw_result de frontend co the list
                        if not isinstance(task.raw_result, dict):
                            task.raw_result = {}
                        task.raw_result["image_paths"] = saved_paths
                        task.raw_result["source"] = "imagen"
                        # Ghi lại resolution thực tế (4K/2K nếu upsample thành công, hoặc "original")
                        if upsample_res:
                            task.raw_result["actual_resolution"] = upsample_res if _upsample_succeeded else "original"
                    else:
                        task.media_id = ""
                        task.output_filename = ""

                    # ── Tăng counter GL project (đếm số ảnh đã tạo) ──
                    _img_count = len(saved_paths) if saved_paths else 1
                    _cache_key = (account_label, task.project_id)
                    _entry = self._gl_project_cache.get(_cache_key)
                    if _entry:
                        _entry["count"] += _img_count
                        logger.info(
                            f"[CreateImage] GL project count: {_entry['count']}/{self.MAX_IMAGES_PER_GL_PROJECT}"
                        )

                    task.status = TaskStatus.COMPLETED
                    # Reset UNUSUAL_ACTIVITY counter khi thành công
                    if task.picked_account_name:
                        self._unusual_activity_counts.pop(task.picked_account_name, None)
                        self._unusual_activity_start_ts.pop(task.picked_account_name, None)
                        # ── Nếu task này có gọi upsample API → mark timestamp để
                        # cooldown cho task sau trên cùng account = 15s (thay vì 10s).
                        if _task_attempted_upsample:
                            self._last_upsample_completed_ts[task.picked_account_name] = time.time()
                            _filelog(
                                f"[CreateImage] Task {task.id} có upsample → "
                                f"cooldown 15s cho account {task.picked_account_name} (thay 10s)"
                            )
                    store.update_video_task(task)
                    logger.info(
                        f"[CreateImage] Task {task.id} DONE. media={task.media_id[:80] if task.media_id else ''}"
                    )
                    return

                except ImagenPermissionError:
                    # 403: reCAPTCHA bị reject hoặc captcha fail
                    # KHÔNG tạo project mới — chỉ log + skip account, để task re-enqueue
                    last_403_error = f"403 reCAPTCHA/Permission ({account_label!r})"
                    logger.warning(
                        f"[CreateImage] 403 ({account_label!r}) — skip account, task sẽ re-enqueue."
                    )
                    # Ghi nhận captcha FAIL
                    try:
                        from core.captcha_pool import get_pool as _get_pool
                        _pool_entry = getattr(task_client, "_last_pool_entry", None)
                        if _pool_entry:
                            _get_pool().report_result(_pool_entry, passed=False)
                            self._record_captcha_stat(account_label, _pool_entry.source, "fail")
                    except Exception:
                        pass
                    # 403 reCAPTCHA → retry captcha liên tục, xoay proxy sau 2 phút
                    rotated_proxy = self._handle_403_retry(
                        account_label, flow="CreateImage-Permission"
                    )
                    if rotated_proxy:
                        task_client.proxy = rotated_proxy
                        logger.info(f"[CreateImage] Hết 2 phút → proxy mới: {rotated_proxy[:40]}")
                    # Luôn retry trên cùng account (không swap)
                    continue

            # Đã hết retry — tất cả account đều thất bại
            tried_real = tried_account_names - {"task-cookie"}

            # ── Case B: Task CHƯA claim được account nào (_resolve_client trả None ngay) ──
            # Xảy ra khi: tất cả account đang busy/cooldown/blacklist tạm thời.
            # KHÔNG phải lỗi account → KHÔNG fail task, chỉ chờ pool rảnh rồi thử lại.
            # (Case này phổ biến sau khi bỏ fallback cookies.json.)
            if len(tried_real) == 0:
                _active_count = len(
                    [a for a in store.list_veo_accounts() if getattr(a, "is_active", False)]
                )
                if _active_count == 0:
                    # Thật sự không có account nào active — lỗi config
                    task.status = TaskStatus.FAILED
                    task.error = (
                        "Pool không có account nào ACTIVE. "
                        "Kiểm tra danh sách account trong admin UI."
                    )
                    store.update_video_task(task)
                    logger.error(
                        f"[CreateImage] Task {task.id}: FAILED — 0 active account in pool"
                    )
                    return
                # Có account active nhưng đang busy/cooldown → re-enqueue chờ 15s, KHÔNG fail.
                task.status = TaskStatus.PENDING
                task.error = (
                    f"Pool tạm thời không khả dụng ({_active_count} account đang "
                    f"busy/cooldown). Auto-retry sau 15s."
                )
                store.update_video_task(task)
                logger.info(
                    f"[CreateImage] Task {task.id}: pool bận (len(tried)=0, "
                    f"active={_active_count}) → chờ 15s rồi re-enqueue"
                )

                def _delayed_pool_wait(_task=task, _self=self):
                    time.sleep(15)
                    try:
                        _check = store.get_video_task(_task.id)
                        if _check and _check.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                            return
                        run_fn = getattr(_self, "_run_create_image", None)
                        if run_fn:
                            _self.enqueue(_task, run_fn)
                    except Exception as _e:
                        logger.error(f"[CreateImage] Pool-wait re-enqueue error: {_e}")

                import threading as _threading
                _threading.Thread(target=_delayed_pool_wait, daemon=True).start()
                return

            # NO-SWAP: outer loop là `while True`, code dưới đây chỉ chạm tới
            # nếu có break/return không-COMPLETED ở 1 nhánh edge case nào đó.
            # KHÔNG re-enqueue, KHÔNG FAIL — task đã được _check_task_timeout
            # set FAILED nếu thực sự timeout. Cleanup nhẹ rồi return.
            logger.warning(
                f"[CreateImage] Task {task.id}: outer loop kết thúc bất thường "
                f"(no-swap mode → đáng lẽ chỉ thoát qua return). Cleanup."
            )
            try:
                if account_locked:
                    self._release_cookie(account_locked)
                    self._clear_activity(account_locked)
            except Exception:
                pass

        except Exception as e:
            tb = ""
            try:
                import traceback as _tb

                tb = _tb.format_exc()
            except Exception:
                tb = repr(e)
            logger.error(
                f"[CreateImage] Task {task.id}: EXCEPTION: {type(e).__name__}: {e}"
            )
            logger.error(f"[CreateImage] Traceback:\n{tb}")
            try:
                task.status = TaskStatus.FAILED
                task.error = f"{type(e).__name__}: {e}"
                store.update_video_task(task)
            except Exception:
                pass
