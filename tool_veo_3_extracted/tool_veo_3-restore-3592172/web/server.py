
import os
import sys
import hashlib

import io as _io

class _SafeStream:
    """Wrapper stream: nếu write ra console crash → ghi ra file fallback, KHÔNG crash."""
    def __init__(self, original, fallback_path):
        self._original = original
        self._fallback = None
        self._fallback_path = fallback_path
    def _get_fallback(self):
        if self._fallback is None:
            os.makedirs(os.path.dirname(self._fallback_path), exist_ok=True)
            self._fallback = open(self._fallback_path, "a", encoding="utf-8", errors="replace")
        return self._fallback
    def write(self, s):
        try:
            self._original.write(s)
        except Exception:
            try:
                self._get_fallback().write(s)
            except Exception:
                pass
    def flush(self):
        try:
            self._original.flush()
        except Exception:
            pass
    def __getattr__(self, name):
        return getattr(self._original, name)

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_log_dir = os.path.join(_root, "logs")
os.makedirs(_log_dir, exist_ok=True)

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.stdout = _SafeStream(sys.stdout, os.path.join(_log_dir, "stdout_fallback.log"))
sys.stderr = _SafeStream(sys.stderr, os.path.join(_log_dir, "stderr_fallback.log"))

import uuid
import time
import logging
import hashlib
import threading
import json
import urllib.error
import urllib.request
from contextlib import contextmanager
from functools import wraps

from flask import Flask, request, jsonify, g, send_file

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from web.models import (
    Project,
    VideoTask,
    VeoModel,
    ScreenRatio,
    ActionType,
    TaskStatus,
    User,
    UserRole,
    Permission,
    UserJobResult,
)
from web.store import store
from web.veo_service import VeoService
from web.exceptions import (
    VietAutoAPIError,
    AuthenticationError,
    ValidationError,
)

_log_format = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

from logging.handlers import RotatingFileHandler
_file_handler = RotatingFileHandler(
    os.path.join(_log_dir, "veo_api.log"),
    maxBytes=10 * 1024 * 1024,  # 10 MB
    backupCount=5,
    encoding="utf-8",
)
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(_log_format)

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setLevel(logging.INFO)
_console_handler.setFormatter(_log_format)

_root_logger = logging.getLogger()
_root_logger.setLevel(logging.DEBUG)
_root_logger.handlers.clear()
_root_logger.addHandler(_file_handler)
_root_logger.addHandler(_console_handler)

logger = logging.getLogger(__name__)

for _noisy in ("pymongo", "pymongo.topology", "pymongo.connection",
               "pymongo.serverSelection", "urllib3", "urllib3.connectionpool",
               "waitress", "waitress.task"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


SPA_BUILD_DIR = os.path.join(ROOT_DIR, "ui", "dist")
MACHINE_STORE_PATH = os.path.join(ROOT_DIR, "data", "machines.json")
MACHINE_ONLINE_TTL_SECONDS = 300
_machine_lock = threading.Lock()

app = Flask(__name__, static_folder=SPA_BUILD_DIR, static_url_path="/assets")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50MB max upload

@app.after_request
def _log_request(response):
    if request.path.startswith("/api/"):
        if request.method == "GET" and response.status_code == 200:
            if request.path in ["/api/veo/queue/settings", "/api/captcha/status", "/api/veo/projects", "/api/veo/videos", "/api/veo/accounts/activity"]:
                return response

    return response

try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address

    def _rate_key():
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        return get_remote_address()

    limiter = Limiter(
        app=app,
        key_func=_rate_key,
        default_limits=["200 per minute"],
        storage_uri="memory://",
    )

    @limiter.request_filter
    def _rate_exempt():
        if request.path.startswith("/captcha/"):
            return True
        if request.path == "/api/veo/video" and request.method == "GET":
            return True
        return False

    VIDEO_RATE_LIMIT = "30 per hour"
except ImportError:
    limiter = None
    VIDEO_RATE_LIMIT = None


_veo_service: VeoService = None


_veo_service_lock = threading.Lock()

def get_veo_service() -> VeoService:
    global _veo_service
    if _veo_service is None:
        with _veo_service_lock:
            if _veo_service is None:
                try:
                    from core.veo_client import VeoClient
                    
                    veo_client = VeoClient("")
                    _veo_service = VeoService(veo_client)
                except Exception as e:
                    raise
    return _veo_service


from web.models import UserRole, Permission


def require_api_key(f):

    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        x_api_key = request.headers.get("X-API-Key", "").strip()
        api_key = request.args.get("token", "")

        if x_api_key:
            api_key = x_api_key
        elif auth_header.startswith("Bearer "):
            api_key = auth_header[len("Bearer ") :].strip()

        if not api_key:
            raise AuthenticationError("Missing API key in header or query param")

        user = store.get_user_by_api_key(api_key)

        master_key = os.environ.get("VIETAUTO_API_KEY", "")
        if master_key and api_key == master_key:
            user = User(
                id="master-dev",
                username="master",
                api_key=master_key,
                password_hash="",
                role=UserRole.ADMIN,
                permissions=[p.value for p in Permission],
            )

        if not user or not user.is_active:
            raise AuthenticationError("Invalid or inactive API key")

        g.user = user
        return f(*args, **kwargs)

    return decorated


def require_role(*roles):

    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not getattr(g, "user", None):
                raise AuthenticationError("User not authenticated")
            if g.user.role not in roles:
                raise VietAutoAPIError(f"Forbidden. Requires role: {roles}", 403)
            return f(*args, **kwargs)

        return decorated_function

    return decorator


def require_permission(permission: str):

    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not getattr(g, "user", None):
                raise AuthenticationError("User not authenticated")
            if g.user.role == UserRole.ADMIN:
                return f(*args, **kwargs)
            if permission not in g.user.permissions:
                raise VietAutoAPIError(
                    f"Forbidden. Missing permission: {permission}", 403
                )
            return f(*args, **kwargs)

        return decorated_function

    return decorator


@app.errorhandler(VietAutoAPIError)
def handle_api_error(e: VietAutoAPIError):
    return jsonify(e.to_dict()), e.status_code


@app.errorhandler(404)
def handle_404(e):
    if request.path.startswith("/api/"):
        return jsonify({"success": False, "error": "Endpoint not found"}), 404
    if os.path.exists(os.path.join(app.static_folder, "index.html")):
        return send_file(os.path.join(app.static_folder, "index.html"))
    return "Not Found", 404


@app.errorhandler(405)
def handle_405(e):
    return jsonify({"success": False, "error": "Method not allowed"}), 405


@app.errorhandler(413)
def handle_413(e):
    return jsonify({"success": False, "error": "File too large (max 50MB)"}), 413


@app.errorhandler(Exception)
def handle_generic(e):
    logger.exception(f"Unhandled error: {e}")
    return jsonify({"success": False, "error": f"Internal server error: {str(e)}"}), 500


def success(data, status_code=200):
    return jsonify({"success": True, "data": data}), status_code


def validate_required(body: dict, *fields):
    missing = [f for f in fields if not body.get(f)]
    if missing:
        raise ValidationError(f"Missing required fields: {', '.join(missing)}")


def validate_enum(value, enum_class, field_name: str):
    valid = [e.value for e in enum_class]
    if value not in valid:
        raise ValidationError(f"Invalid {field_name}='{value}'. Valid values: {valid}")


def safe_int(value, default=0):
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _now_ts():
    return int(time.time())


def _load_machines():
    if not os.path.exists(MACHINE_STORE_PATH):
        return {}
    try:
        with open(MACHINE_STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.exception("Failed to load machine registry")
        return {}


def _save_machines(machines):
    os.makedirs(os.path.dirname(MACHINE_STORE_PATH), exist_ok=True)
    tmp_path = MACHINE_STORE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(machines, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, MACHINE_STORE_PATH)


def _machine_required_str(body, key):
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"Missing required field: {key}")
    return value.strip()


def _is_machine_online(machine, ts=None):
    ts = ts or _now_ts()
    last_seen = safe_int(machine.get("last_heartbeat_at"), 0)
    return last_seen > 0 and (ts - last_seen) <= MACHINE_ONLINE_TTL_SECONDS


def _machine_public_view(machine, ts=None):
    ts = ts or _now_ts()
    item = dict(machine)
    item.pop("machine_secret", None)
    is_online = _is_machine_online(machine, ts)
    item["computed_status"] = "online" if is_online else "offline"
    item["is_online"] = is_online
    last_seen = safe_int(machine.get("last_heartbeat_at"), 0)
    item["heartbeat_age_seconds"] = ts - last_seen if last_seen else None
    return item


def _machine_summary(items):
    return {
        "total": len(items),
        "online": sum(1 for m in items if m.get("is_online")),
        "accepting": sum(1 for m in items if m.get("is_online") and m.get("accepting_jobs")),
        "available_slots": sum(safe_int(m.get("available_slots"), 0) for m in items if m.get("is_online")),
        "running_jobs": sum(safe_int(m.get("running_jobs"), 0) for m in items if m.get("is_online")),
    }


def _require_machine_auth(body):
    machine_id = _machine_required_str(body, "machine_id")
    machine_secret = _machine_required_str(body, "machine_secret")
    with _machine_lock:
        machines = _load_machines()
        machine = machines.get(machine_id)
    if not machine:
        raise VietAutoAPIError("Machine not registered", 404)
    if str(machine.get("machine_secret") or "") != machine_secret:
        raise AuthenticationError("Invalid machine_secret")
    return machine_id, machine


def _supported_action_types(supported_modes):
    modes = {str(x).strip().lower() for x in (supported_modes or []) if str(x).strip()}
    if not modes:
        modes = {"image", "video"}
    action_types = []
    if "video" in modes:
        action_types.extend([
            "TEXT_TO_VIDEO",
            "IMAGE_TO_VIDEO",
            "IMAGES_TO_VIDEO",
            "FRAMES_TO_VIDEO",
            "REFERENCE_IMAGE_TO_VIDEO",
        ])
    if "image" in modes:
        action_types.extend(["CREATE_IMAGE", "IMAGES_TO_IMAGE"])
    return list(dict.fromkeys(action_types))


def _machine_job_contract(task):
    raw = task.raw_result if isinstance(getattr(task, "raw_result", None), dict) else {}
    worker_claim = raw.get("worker_claim") if isinstance(raw.get("worker_claim"), dict) else {}
    image_paths = raw.get("image_paths") or raw.get("reference_paths") or []
    if not isinstance(image_paths, list):
        image_paths = [image_paths]
    return {
        "job_id": task.id,
        "action_type": task.action_type,
        "prompts": task.prompts or [],
        "model": task.model,
        "screen_ratio": task.screen_ratio,
        "upsample_resolution": raw.get("upsample_resolution"),
        "image_paths": image_paths,
        "attempts": safe_int(worker_claim.get("attempts"), 0),
    }


@app.route("/", methods=["GET"])
def index():
    _dist_index = os.path.join(ROOT_DIR, "ui", "dist", "index.html")
    if os.path.exists(_dist_index):
        return send_file(_dist_index)

    return jsonify(
        {
            "name": "Custom VietAuto-style API Server",
            "version": "1.0.0",
            "docs": "/api/docs",
            "status": "running",
            "message": "SPA not built yet (Missing ui/dist/index.html)",
        }
    )


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/api/machines", methods=["GET"])
def list_machines():
    ts = _now_ts()
    with _machine_lock:
        machines = _load_machines()
    items = [_machine_public_view(machine, ts) for machine in machines.values()]
    items.sort(key=lambda m: m.get("last_heartbeat_at") or m.get("updated_at") or 0, reverse=True)
    return success({"machines": items, "summary": _machine_summary(items)})


def _machine_lookup_response(machine_id: str):
    machine_id = str(machine_id or "").strip()
    if not machine_id:
        raise ValidationError("Missing required field: machine_id")
    ts = _now_ts()
    with _machine_lock:
        machines = _load_machines()
        machine = machines.get(machine_id)
    return success({
        "machine_id": machine_id,
        "registered": bool(machine),
        "machine": _machine_public_view(machine, ts) if machine else None,
    })


@app.route("/api/machines/check", methods=["GET", "POST"])
def check_machine_registered():
    body = request.get_json(silent=True) or {}
    machine_id = body.get("machine_id") or request.args.get("machine_id")
    return _machine_lookup_response(machine_id)


@app.route("/api/machines/<machine_id>", methods=["GET"])
def get_machine(machine_id):
    return _machine_lookup_response(machine_id)


@app.route("/api/machines/<machine_id>", methods=["DELETE"])
def delete_machine(machine_id):
    machine_id = str(machine_id or "").strip()
    if not machine_id:
        raise ValidationError("Missing required field: machine_id")
    with _machine_lock:
        machines = _load_machines()
        removed = machines.pop(machine_id, None)
        if removed is None:
            raise VietAutoAPIError("Machine not registered", 404)
        _save_machines(machines)
    return success({
        "deleted": True,
        "machine_id": machine_id,
        "machine": _machine_public_view(removed, _now_ts()),
    })


@app.route("/api/machines/register", methods=["POST"])
def register_machine():
    body = request.get_json(silent=True) or {}
    machine_id = _machine_required_str(body, "machine_id")
    machine_secret = _machine_required_str(body, "machine_secret")
    public_url = _machine_required_str(body, "public_url")
    version = _machine_required_str(body, "version")
    ts = _now_ts()

    with _machine_lock:
        machines = _load_machines()
        current = machines.get(machine_id, {})
        current.update({
            "machine_id": machine_id,
            "machine_secret": machine_secret,
            "public_url": public_url,
            "version": version,
            "api_key_hash": str(body.get("api_key_hash") or current.get("api_key_hash") or "").strip(),
            "supported_modes": body.get("supported_modes") if isinstance(body.get("supported_modes"), list) else current.get("supported_modes", ["image", "video"]),
            "public_api_auth_mode": str(body.get("public_api_auth_mode") or current.get("public_api_auth_mode") or "machine_secret").strip(),
            "public_api_auth_token": str(body.get("public_api_auth_token") or current.get("public_api_auth_token") or "").strip(),
            "machine_api_key": str(body.get("machine_api_key") or current.get("machine_api_key") or "").strip(),
            "machine_api_key_hash": str(body.get("machine_api_key_hash") or current.get("machine_api_key_hash") or "").strip(),
            "status": current.get("status") or "registered",
            "token_count": safe_int(current.get("token_count"), 0),
            "max_concurrent_jobs": safe_int(body.get("max_concurrent_jobs"), safe_int(current.get("max_concurrent_jobs"), 0)),
            "available_slots": safe_int(current.get("available_slots"), 0),
            "running_jobs": safe_int(current.get("running_jobs"), 0),
            "accepting_jobs": bool(current.get("accepting_jobs", False)),
            "frp_status": current.get("frp_status") or "unknown",
            "recent_error_count": safe_int(current.get("recent_error_count"), 0),
            "cooldown_until": current.get("cooldown_until"),
            "avg_duration_seconds": current.get("avg_duration_seconds"),
            "last_error": current.get("last_error"),
            "registered_at": current.get("registered_at") or ts,
            "updated_at": ts,
            "last_heartbeat_at": current.get("last_heartbeat_at"),
        })
        machines[machine_id] = current
        _save_machines(machines)

    return success({"machine": _machine_public_view(current, ts)}, 201)


@app.route("/api/machines/heartbeat", methods=["POST"])
def heartbeat_machine():
    body = request.get_json(silent=True) or {}
    machine_id = _machine_required_str(body, "machine_id")
    ts = _now_ts()

    with _machine_lock:
        machines = _load_machines()
        if machine_id not in machines:
            raise VietAutoAPIError("Machine not registered", 404)
        machine = machines[machine_id]
        machine.update({
            "public_url": str(body.get("public_url") or machine.get("public_url") or "").strip(),
            "status": str(body.get("status") or "online").strip(),
            "api_key_hash": str(body.get("api_key_hash") or machine.get("api_key_hash") or "").strip(),
            "machine_api_key_hash": str(body.get("machine_api_key_hash") or machine.get("machine_api_key_hash") or "").strip(),
            "supported_modes": body.get("supported_modes") if isinstance(body.get("supported_modes"), list) else machine.get("supported_modes", ["image", "video"]),
            "token_count": safe_int(body.get("token_count"), safe_int(machine.get("token_count"), 0)),
            "max_concurrent_jobs": safe_int(body.get("max_concurrent_jobs"), safe_int(machine.get("max_concurrent_jobs"), 0)),
            "available_slots": safe_int(body.get("available_slots"), safe_int(machine.get("available_slots"), 0)),
            "running_jobs": safe_int(body.get("running_jobs"), safe_int(machine.get("running_jobs"), 0)),
            "accepting_jobs": bool(body.get("accepting_jobs", machine.get("accepting_jobs", False))),
            "frp_status": str(body.get("frp_status") or machine.get("frp_status") or "unknown").strip(),
            "recent_error_count": safe_int(body.get("recent_error_count"), safe_int(machine.get("recent_error_count"), 0)),
            "cooldown_until": body.get("cooldown_until", machine.get("cooldown_until")),
            "avg_duration_seconds": body.get("avg_duration_seconds", machine.get("avg_duration_seconds")),
            "last_error": str(body.get("last_error") or machine.get("last_error") or "").strip(),
            "updated_at": ts,
            "last_heartbeat_at": ts,
        })
        machines[machine_id] = machine
        _save_machines(machines)

    return success({"machine": _machine_public_view(machine, ts)})


@app.route("/api/machines/jobs/claim", methods=["POST"])
def claim_machine_jobs():
    body = request.get_json(silent=True) or {}
    machine_id, machine = _require_machine_auth(body)
    available_slots = max(0, safe_int(body.get("available_slots"), safe_int(machine.get("available_slots"), 0)))
    running_jobs = max(0, safe_int(body.get("running_jobs"), safe_int(machine.get("running_jobs"), 0)))
    supported_modes = body.get("supported_modes") if isinstance(body.get("supported_modes"), list) else []
    supported_action_types = _supported_action_types(supported_modes)
    ts = _now_ts()

    with _machine_lock:
        machines = _load_machines()
        current = machines.get(machine_id, machine)
        current.update({
            "available_slots": available_slots,
            "running_jobs": running_jobs,
            "accepting_jobs": bool(body.get("accepting_jobs", current.get("accepting_jobs", True))),
            "supported_modes": supported_modes or ["image", "video"],
            "status": "online",
            "updated_at": ts,
            "last_heartbeat_at": ts,
        })
        machines[machine_id] = current
        _save_machines(machines)

    jobs = []
    if available_slots > 0 and current.get("accepting_jobs", True):
        claimed = store.claim_machine_jobs(
            machine_id=machine_id,
            available_slots=available_slots,
            supported_action_types=supported_action_types,
        )
        jobs = [_machine_job_contract(task) for task in claimed]

    return success({"jobs": jobs})


@app.route("/api/machines/jobs/<job_id>/result", methods=["POST"])
def save_machine_job_result(job_id):
    body = request.get_json(silent=True) or {}
    machine_id, _machine = _require_machine_auth(body)
    status = str(body.get("status") or "").strip().lower()
    if status not in ("completed", "failed"):
        raise ValidationError("status must be completed or failed")
    task = store.save_machine_job_result(
        job_id=job_id,
        machine_id=machine_id,
        status=status,
        download_url=body.get("download_url"),
        duration_seconds=body.get("duration_seconds"),
        attempts=body.get("attempts"),
        error=body.get("error"),
    )
    if not task:
        raise VietAutoAPIError("Job not found or not assigned to this machine", 404)
    return success({"message": "Job result saved", "job": task.to_public_dict()})


_PUBLIC_DISPATCH_ACTION_ENDPOINTS = {
    "CREATE_IMAGE": "/api/veo/images",
    "IMAGES_TO_IMAGE": "/api/veo/images/from-images",
    "TEXT_TO_VIDEO": "/api/veo/videos/from-text",
    "IMAGE_TO_VIDEO": "/api/veo/videos/from-start-image",
    "IMAGES_TO_VIDEO": "/api/veo/videos/from-start-end-images",
    "FRAMES_TO_VIDEO": "/api/veo/videos/from-start-end-images",
    "REFERENCE_IMAGE_TO_VIDEO": "/api/veo/videos/from-reference-image",
}
_PUBLIC_DISPATCH_STATS = {"last_dispatch_at": None, "last_poll_at": None, "dispatch_count": 0, "poll_count": 0, "last_error": None}


def _public_action_value(action_type) -> str:
    value = getattr(action_type, "value", action_type)
    if isinstance(value, str) and value.startswith("ActionType."):
        value = value.split(".", 1)[1]
    return str(value or "").strip()


def _public_dispatch_mode_for_action(action_type: str) -> str:
    return "image" if _public_action_value(action_type) in ("CREATE_IMAGE", "IMAGES_TO_IMAGE") else "video"


def _public_task_payload(task: VideoTask) -> dict:
    raw = task.raw_result if isinstance(task.raw_result, dict) else {}
    image_paths = raw.get("image_paths") or raw.get("reference_paths") or task.image_refs or []
    if not isinstance(image_paths, list):
        image_paths = [image_paths]
    end_image_paths = raw.get("end_image_paths") or raw.get("end_reference_paths") or []
    if not isinstance(end_image_paths, list):
        end_image_paths = [end_image_paths]
    images_b64 = raw.get("images_b64") or raw.get("images") or []
    if not isinstance(images_b64, list):
        images_b64 = [images_b64]

    portable_images = []
    public_image_paths = []

    def _append_local_image(path: str, meta: dict | None = None):
        try:
            import base64 as _base64
            import mimetypes as _mimetypes
            with open(path, "rb") as _fh:
                portable_images.append({
                    "b64": _base64.b64encode(_fh.read()).decode("ascii"),
                    "mime": (meta or {}).get("mime") or _mimetypes.guess_type(path)[0] or "image/jpeg",
                    "name": (meta or {}).get("name") or os.path.basename(path),
                })
            return True
        except Exception as exc:
            logger.warning("[PublicDispatch] encode image failed task=%s path=%s error=%s", task.id, path, exc)
            return False

    def _append_image_ref(item, *, allow_public_path: bool = True):
        if isinstance(item, dict):
            if item.get("b64") or item.get("url"):
                portable_images.append(item)
                return
            path = str(item.get("path") or item.get("local_path") or item.get("file_path") or item.get("url") or "").strip()
        else:
            raw_value = str(item or "").strip()
            if raw_value.lower().startswith("data:"):
                portable_images.append(raw_value)
                return
            path = raw_value
        if not path:
            return
        if path.lower().startswith(("http://", "https://")):
            if allow_public_path:
                public_image_paths.append(path)
            else:
                portable_images.append({"url": path})
            return
        if os.path.exists(path):
            _append_local_image(path, item if isinstance(item, dict) else None)
        else:
            # A local path from Natha is not valid on the Banana Worker machine.
            # Do not forward it; fail fast at worker payload level with a clear log.
            logger.warning("[PublicDispatch] skip missing local image task=%s path=%s", task.id, path)

    for item in images_b64:
        _append_image_ref(item, allow_public_path=False)
    for item in image_paths:
        _append_image_ref(item, allow_public_path=True)
    for item in end_image_paths:
        _append_image_ref(item, allow_public_path=False)

    action_type = _public_action_value(task.action_type)
    payload = {
        "action_type": action_type,
        "prompts": task.prompts or [],
        "model": task.model,
        "screen_ratio": task.screen_ratio,
        "image_paths": public_image_paths,
    }
    if portable_images:
        payload["images_b64"] = portable_images
        payload["image_paths"] = []
    if raw.get("upsample_resolution"):
        payload["upsample_resolution"] = raw.get("upsample_resolution")
    return payload


def _public_machine_resolve_api_key(machine: dict) -> str:
    configured = str(machine.get("machine_api_key") or "").strip()
    if configured:
        return configured
    hash_candidates = [
        str(machine.get("machine_api_key_hash") or "").strip(),
        str(machine.get("api_key_hash") or "").strip(),
    ]
    for wanted_hash in [h for h in hash_candidates if h]:
        try:
            for user in store.list_users():
                api_key = str(getattr(user, "api_key", "") or "").strip()
                if api_key and hashlib.sha256(api_key.encode("utf-8")).hexdigest() == wanted_hash:
                    return api_key
        except Exception as exc:
            logger.warning("[PublicDispatch] resolve machine api key failed hash=%s error=%s", wanted_hash[:8], exc)
    # Backward compatible fallback for machines that have not yet sent
    # machine_api_key_hash in heartbeat/register. The default admin key is
    # created by MongoStore on every install unless changed by the operator.
    fallback_keys = [
        os.environ.get("VIETAUTO_API_KEY", ""),
        os.environ.get("PUBLIC_MACHINE_API_KEY", ""),
        "admin-secret-key",
    ]
    for api_key in fallback_keys:
        api_key = str(api_key or "").strip()
        if api_key:
            return api_key
    return ""


def _public_request_api_key() -> str:
    try:
        x_api_key = request.headers.get("X-API-Key", "").strip()
        auth_header = request.headers.get("Authorization", "")
        query_token = request.args.get("token", "").strip()
        if x_api_key:
            return x_api_key
        if auth_header.startswith("Bearer "):
            return auth_header[len("Bearer ") :].strip()
        if query_token:
            return query_token
        payload = request.get_json(silent=True) if request.is_json else None
        if isinstance(payload, dict):
            for key in ("api_key", "token", "x_api_key", "machine_api_key"):
                value = str(payload.get(key) or "").strip()
                if value:
                    return value
    except RuntimeError:
        return ""
    except Exception as exc:
        logger.warning("[PublicDispatch] read request api key failed: %s", exc)
    return ""


def _public_machine_headers(machine: dict, task_id: str) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Idempotency-Key": task_id,
    }
    token = str(machine.get("public_api_auth_token") or "").strip()
    machine_secret = str(machine.get("machine_secret") or "").strip()
    machine_api_key = _public_request_api_key() or _public_machine_resolve_api_key(machine)
    if machine_secret:
        headers["X-Machine-Secret"] = machine_secret
    if machine_api_key:
        headers["X-API-Key"] = machine_api_key
        logger.info("[PublicDispatch] using X-API-Key for machine dispatch source=%s", "request" if _public_request_api_key() else "machine/fallback")
    else:
        logger.warning("[PublicDispatch] no X-API-Key resolved for machine dispatch machine=%s", machine.get("machine_id"))
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _public_machine_effective_slots(machine: dict, processing_counts: dict) -> int:
    max_jobs = safe_int(machine.get("max_concurrent_jobs"), 0)
    heartbeat_available = safe_int(machine.get("available_slots"), 0)
    running_jobs = safe_int(machine.get("running_jobs"), 0)
    if max_jobs <= 0:
        max_jobs = max(heartbeat_available + running_jobs, 1 if machine.get("accepting_jobs", False) else 0)
    natha_processing = processing_counts.get(machine.get("machine_id"), 0)
    available = heartbeat_available if heartbeat_available > 0 else max_jobs - running_jobs
    if machine.get("accepting_jobs", False) and available <= 0 and natha_processing < max_jobs:
        available = 1
    if max_jobs > 0:
        return max(0, min(available, max_jobs - natha_processing))
    return max(0, available)


def _public_processing_counts() -> dict:
    counts = {}
    for task in store.list_public_processing_tasks(limit=1000):
        raw = task.raw_result if isinstance(task.raw_result, dict) else {}
        dispatch = raw.get("public_dispatch") if isinstance(raw.get("public_dispatch"), dict) else {}
        machine_id = dispatch.get("assigned_machine_id")
        if machine_id:
            counts[machine_id] = counts.get(machine_id, 0) + 1
    return counts


def _select_public_machine(task: VideoTask) -> dict | None:
    mode = _public_dispatch_mode_for_action(task.action_type)
    raw = task.raw_result if isinstance(task.raw_result, dict) else {}
    wanted_hash = str(raw.get("api_key_hash") or "").strip()
    now = _now_ts()
    processing_counts = _public_processing_counts()
    with _machine_lock:
        machines = list(_load_machines().values())
    candidates = []
    for machine in machines:
        if not _machine_public_view(machine, now).get("is_online"):
            continue
        if not machine.get("accepting_jobs", False):
            continue
        if machine.get("cooldown_until") and safe_int(machine.get("cooldown_until"), 0) > now:
            continue
        supported = machine.get("supported_modes") if isinstance(machine.get("supported_modes"), list) else ["image", "video"]
        if mode not in {str(x).lower() for x in supported}:
            continue
        machine_id = machine.get("machine_id")
        natha_processing = processing_counts.get(machine_id, 0)
        slots = _public_machine_effective_slots(machine, processing_counts)
        if slots <= 0:
            continue
        same_key = bool(wanted_hash and wanted_hash == str(machine.get("api_key_hash") or "").strip())
        idle_bonus = 10000 if natha_processing == 0 else 0
        score = (
            idle_bonus
            + (1000 if same_key else 0)
            + slots * 10
            - natha_processing * 500
            - safe_int(machine.get("running_jobs"), 0) * 2
            - safe_int(machine.get("recent_error_count"), 0) * 20
        )
        candidates.append((score, slots, machine))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][2]


def _call_public_banana_create(task: VideoTask, machine: dict) -> dict:
    machine_id = str(machine.get("machine_id") or "").strip()
    if not machine_id:
        raise ValidationError("Selected machine has no machine_id")

    action_type = _public_action_value(task.action_type)
    endpoint = _PUBLIC_DISPATCH_ACTION_ENDPOINTS.get(action_type)
    if not endpoint:
        raise ValidationError(f"Unsupported action_type for public dispatch: {task.action_type}")

    public_url = str(machine.get("public_url") or "").strip().rstrip("/")
    if not public_url:
        raise ValidationError(f"Selected machine {machine_id} has no public_url")

    url = f"{public_url}{endpoint}"
    payload = _public_task_payload(task)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = _public_machine_headers(machine, task.id)
    last_error = None

    for attempt in range(1, 4):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            logger.info(
                "[PublicDispatch] POST task=%s machine=%s endpoint=%s attempt=%s",
                task.id,
                machine_id,
                endpoint,
                attempt,
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                resp_body = resp.read().decode("utf-8", errors="replace")
                response = json.loads(resp_body) if resp_body else {}
        except urllib.error.HTTPError as exc:
            error_body = ""
            try:
                error_body = exc.read().decode("utf-8", errors="replace")[:1000]
            except Exception:
                pass
            last_error = f"HTTP {exc.code}: {error_body or exc.reason}"
            logger.warning(
                "[PublicDispatch] create HTTP failed task=%s machine=%s status=%s attempt=%s error=%s",
                task.id,
                machine_id,
                exc.code,
                attempt,
                last_error,
            )
            if exc.code < 500 or attempt >= 3:
                raise VietAutoAPIError(f"Public Banana create failed: {last_error}", 502)
            time.sleep(min(2 * attempt, 5))
            continue
        except Exception as exc:
            last_error = str(exc)
            logger.warning(
                "[PublicDispatch] create network failed task=%s machine=%s attempt=%s error=%s",
                task.id,
                machine_id,
                attempt,
                last_error,
            )
            if attempt >= 3:
                raise VietAutoAPIError(f"Public Banana create failed: {last_error}", 502)
            time.sleep(min(2 * attempt, 5))
            continue

        if isinstance(response, dict) and response.get("success") is False:
            last_error = response.get("error") or response.get("message") or "Worker returned success=false"
            raise VietAutoAPIError(f"Public Banana create failed: {last_error}", 502)

        data = response.get("data") if isinstance(response, dict) and isinstance(response.get("data"), dict) else response
        remote_task_id = ""
        if isinstance(data, dict):
            remote_task_id = str(data.get("id") or data.get("task_id") or data.get("remote_task_id") or "").strip()
        if not remote_task_id:
            raise VietAutoAPIError("Public Banana create response missing data.id", 502)

        return {
            "remote_task_id": remote_task_id,
            "dispatch_endpoint": endpoint,
            "public_url": public_url,
            "response": response,
        }

    raise VietAutoAPIError(f"Public Banana create failed: {last_error or 'unknown error'}", 502)


def _public_dispatch_tasks(tasks: list[VideoTask]) -> dict:
    dispatched = []
    skipped = []
    for task in tasks:
        machine = _select_public_machine(task)
        if not machine:
            skipped.append({"job_id": task.id, "reason": "no_machine"})
            continue
        try:
            create_result = _call_public_banana_create(task, machine)
            store.mark_public_dispatching(
                task_id=task.id,
                machine_id=machine.get("machine_id"),
                metadata={
                    "remote_task_id": create_result["remote_task_id"],
                    "public_url": create_result["public_url"],
                    "dispatch_endpoint": create_result["dispatch_endpoint"],
                    "idempotency_key": task.id,
                    "create_response": create_result["response"],
                },
            )
            dispatched.append({"job_id": task.id, "machine_id": machine.get("machine_id"), "remote_task_id": create_result["remote_task_id"]})
        except Exception as exc:
            logger.exception("[PublicDispatch] dispatch failed task=%s error=%s", task.id, exc)
            skipped.append({"job_id": task.id, "reason": str(exc)})
    return {"dispatched": dispatched, "skipped": skipped}


def _public_dispatch_run_once(limit: int = 25) -> dict:
    result = _public_dispatch_tasks(store.list_public_dispatch_candidates(limit=limit))
    _PUBLIC_DISPATCH_STATS.update({"last_dispatch_at": _now_ts(), "dispatch_count": _PUBLIC_DISPATCH_STATS.get("dispatch_count", 0) + len(result.get("dispatched", []))})
    return result


def _public_poll_task(task: VideoTask) -> dict:
    raw = task.raw_result if isinstance(task.raw_result, dict) else {}
    dispatch = raw.get("public_dispatch") if isinstance(raw.get("public_dispatch"), dict) else {}
    remote_task_id = dispatch.get("remote_task_id")
    public_url = str(dispatch.get("public_url") or "").rstrip("/")
    machine_id = dispatch.get("assigned_machine_id")
    if not remote_task_id or not public_url:
        return {"job_id": task.id, "polled": False, "reason": "missing_remote"}
    with _machine_lock:
        machine = _load_machines().get(machine_id, {}) if machine_id else {}
    url = f"{public_url}/api/veo/tasks/{remote_task_id}"
    req = urllib.request.Request(url, headers=_public_machine_headers(machine, task.id), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body) if body else {}
    except Exception as exc:
        logger.warning("[PublicDispatch] poll failed task=%s remote=%s error=%s", task.id, remote_task_id, exc)
        return {"job_id": task.id, "remote_task_id": remote_task_id, "polled": False, "reason": str(exc)}
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    remote_status = payload.get("status") or ""
    store.save_public_dispatch_poll_result(task_id=task.id, remote_status=remote_status, result_payload=payload)
    return {"job_id": task.id, "remote_task_id": remote_task_id, "polled": True, "status": remote_status}


def _public_poll_run_once(limit: int = 100) -> dict:
    results = [_public_poll_task(task) for task in store.list_public_processing_tasks(limit=limit)]
    _PUBLIC_DISPATCH_STATS.update({"last_poll_at": _now_ts(), "poll_count": _PUBLIC_DISPATCH_STATS.get("poll_count", 0) + len(results)})
    return {"results": results}


@app.route("/api/machines/dispatch/run-once", methods=["POST"])
def public_dispatch_run_once_endpoint():
    body = request.get_json(silent=True) or {}
    return success(_public_dispatch_run_once(limit=max(1, min(safe_int(body.get("limit"), 25), 100))))


@app.route("/api/machines/dispatch/poll-once", methods=["POST"])
def public_dispatch_poll_once_endpoint():
    body = request.get_json(silent=True) or {}
    return success(_public_poll_run_once(limit=max(1, min(safe_int(body.get("limit"), 100), 500))))


@app.route("/api/machines/dispatch/status", methods=["GET"])
def public_dispatch_status_endpoint():
    return success(dict(_PUBLIC_DISPATCH_STATS))


@app.route("/api/docs/view", methods=["GET"])
@require_api_key
def api_docs_view():
    docs_path = os.path.join(ROOT_DIR, "web", "_api_docs_secure.html")
    if not os.path.exists(docs_path):
        return jsonify({"success": False, "error": "API docs not found"}), 404
    return send_file(docs_path, mimetype="text/html")


@app.route("/api/me", methods=["GET"])
@require_api_key
def get_me():
    user = g.user
    data = {
        "api_key": user.api_key[:8] + "...",
        "username": user.username,
        "role": user.role,
        "permissions": user.permissions,
        "status": "active" if user.is_active else "inactive",
    }
    return success(data)


@app.route("/api/check-user-key", methods=["GET", "POST"])
def check_user_key():
    body = request.get_json(silent=True) or {}
    key = (body.get("key") or request.args.get("key") or "").strip()
    if not key:
        raise ValidationError("Missing required field: key")

    user = store.get_user_by_api_key(key)
    if not user:
        return success({
            "exists": False,
            "valid": False,
            "active": False,
            "user": None,
        })

    return success({
        "exists": True,
        "valid": bool(user.is_active),
        "active": bool(user.is_active),
        "user": {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "permissions": user.permissions,
            "status": "active" if user.is_active else "inactive",
        },
    })


@app.route("/api/check-api-key", methods=["GET", "POST"])
def check_api_key_exists():
    body = request.get_json(silent=True) or {}
    key = (
        request.headers.get("X-API-Key", "").strip()
        or (body.get("api_key") or body.get("key") or "").strip()
        or request.args.get("api_key", "").strip()
        or request.args.get("key", "").strip()
    )
    user = store.get_user_by_api_key(key) if key else None
    return success({
        "exists": bool(user),
        "active": bool(user and user.is_active),
    })


@app.route("/api/veo/account-session", methods=["POST"])
@require_api_key
def issue_veo_account_session():
    from web.account_session_api import account_session_api

    data = account_session_api.issue_for_user(g.user)
    return success(data)


@app.route("/api/get-token-project", methods=["GET", "POST"])
def get_token_project_by_api_key():
    body = request.get_json(silent=True) or {}
    key = (
        request.headers.get("X-API-Key", "").strip()
        or (body.get("api_key") or body.get("key") or "").strip()
        or request.args.get("api_key", "").strip()
        or request.args.get("key", "").strip()
    )
    user = store.get_user_by_api_key(key) if key else None
    if not user or not user.is_active:
        raise AuthenticationError("Invalid or inactive API key")

    from web.account_session_api import account_session_api

    return success(account_session_api.issue_for_user(user))


@app.route("/api/admin/veo/account-sessions", methods=["GET"])
@require_api_key
@require_role(UserRole.ADMIN)
def list_all_veo_account_sessions():
    from web.account_session_api import account_session_api

    items = []
    for account in store.list_veo_accounts():
        if not account:
            continue
        account_name = getattr(account, "name", "")
        if not getattr(account, "is_active", False):
            items.append({"account_name": account_name, "ok": False, "error": "inactive account"})
            continue
        if not (getattr(account, "cookie", None) or "").strip():
            items.append({"account_name": account_name, "ok": False, "error": "missing cookie"})
            continue
        try:
            data = account_session_api.issue_for_account_name(account_name, user=g.user)
            items.append({
                "account_name": data.get("account_name") or account_name,
                "token": data.get("token"),
                "project_id": data.get("project_id"),
                "proxy": getattr(account, "static_proxy", None) or getattr(account, "proxy", None) or "",
                "ok": True,
            })
        except Exception as exc:
            items.append({"account_name": account_name, "ok": False, "error": str(exc)})

    return success({
        "total": len(items),
        "success": sum(1 for item in items if item.get("ok")),
        "failed": sum(1 for item in items if not item.get("ok")),
        "failed_emails": [item.get("account_name") for item in items if not item.get("ok")],
        "items": items,
    })


@app.route("/api/user-job-results", methods=["POST"])
@require_api_key
def upsert_user_job_results():
    body = request.get_json(silent=True)
    if not isinstance(body, list):
        raise ValidationError("Request body must be a JSON array")
    if not body:
        raise ValidationError("Request body must contain at least 1 item")

    required_fields = ("project_name", "job_id", "prompt", "status")
    saved = []
    errors = []
    created_count = 0
    updated_count = 0

    for idx, item in enumerate(body):
        if not isinstance(item, dict):
            errors.append({"index": idx, "error": "Item must be an object"})
            continue
        missing = [field for field in required_fields if not item.get(field)]
        if missing:
            errors.append({"index": idx, "job_id": item.get("job_id"), "error": f"Missing required fields: {', '.join(missing)}"})
            continue

        try:
            duration = item.get("duration_seconds")
            if duration is not None:
                duration = float(duration)
        except (TypeError, ValueError):
            errors.append({"index": idx, "job_id": item.get("job_id"), "error": "duration_seconds must be a number or null"})
            continue

        submitted_api_key = str(item.get("api_key") or item.get("key") or g.user.api_key or "").strip()
        result = UserJobResult(
            id=str(uuid.uuid4()),
            user_id=g.user.id,
            project_name=str(item.get("project_name", "")).strip(),
            job_id=str(item.get("job_id", "")).strip(),
            prompt=str(item.get("prompt", "")),
            status=str(item.get("status", "")).strip(),
            api_key=submitted_api_key,
            download_url=item.get("download_url"),
            duration_seconds=duration,
            error=item.get("error"),
        )
        stored, created = store.upsert_user_job_result(result)
        if created:
            created_count += 1
        else:
            updated_count += 1
        saved.append(stored.to_public_dict())

    return success({
        "received": len(body),
        "saved": len(saved),
        "created": created_count,
        "updated": updated_count,
        "failed": len(errors),
        "errors": errors,
        "items": saved,
    }, 200 if not errors else 207)


@app.route("/api/user-job-results", methods=["GET"])
@require_api_key
def list_user_job_results():
    is_admin = getattr(g.user, "role", None) == UserRole.ADMIN
    user_id = request.args.get("user_id") if is_admin else g.user.id
    project_name = (request.args.get("project_name") or "").strip() or None
    status = (request.args.get("status") or "").strip() or None
    search = (request.args.get("search") or "").strip() or None
    skip = safe_int(request.args.get("skip"), 0)
    limit = safe_int(request.args.get("limit"), 100)
    limit = max(1, min(limit, 500))

    results, total = store.list_user_job_results(
        user_id=user_id,
        project_name=project_name,
        status=status,
        search=search,
        skip=max(0, skip),
        limit=limit,
    )
    items = []
    user_cache = {}
    for result in results:
        item = result.to_public_dict()
        raw_api_key = (getattr(result, "api_key", None) or "").strip()
        owner = None
        if raw_api_key:
            owner = user_cache.get(raw_api_key)
            if raw_api_key not in user_cache:
                owner = store.get_user_by_api_key(raw_api_key)
                user_cache[raw_api_key] = owner
        if not owner:
            owner = user_cache.get(result.user_id)
            if result.user_id not in user_cache:
                owner = store.get_user(result.user_id)
                user_cache[result.user_id] = owner

        if owner and getattr(owner, "api_key", None):
            api_key = owner.api_key or raw_api_key
            item["api_key"] = api_key[:8] + "..."
            item["username"] = owner.username or "—"
        elif raw_api_key:
            item["api_key"] = raw_api_key[:8] + "..."
            item["username"] = "—"
        else:
            item["api_key"] = "—"
            item["username"] = "—"
        try:
            item["created_time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(item.get("created_at") or 0)))
        except Exception:
            item["created_time"] = ""
        try:
            item["updated_time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(item.get("updated_at") or 0)))
        except Exception:
            item["updated_time"] = ""
        items.append(item)

    return success({
        "items": items,
        "total": total,
        "skip": skip,
        "limit": limit,
    })


@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    from werkzeug.security import check_password_hash

    body = request.get_json(silent=True) or {}
    validate_required(body, "username", "password")

    user = store.get_user_by_username(body["username"])
    if not user or not check_password_hash(user.password_hash, body["password"]):
        raise AuthenticationError("Sai tên đăng nhập hoặc mật khẩu")

    if not user.is_active:
        raise AuthenticationError("Tài khoản đã bị khoá")

    return success(
        {
            "username": user.username,
            "role": user.role,
            "permissions": user.permissions,
            "token": user.api_key,
        }
    )


@app.route("/api/admin/users", methods=["GET"])
@require_api_key
@require_role(UserRole.ADMIN)
def list_users():
    users = store.list_users()
    result = []
    for u in users:
        item = u.to_public_dict()
        assigned_accounts = store.list_veo_accounts_for_user(u.id)
        item["assigned_veo_account_ids"] = [a.id for a in assigned_accounts]
        item["assigned_veo_account_names"] = [a.name for a in assigned_accounts]
        result.append(item)
    return success(result)


@app.route("/api/admin/users", methods=["POST"])
@require_api_key
@require_role(UserRole.ADMIN)
def create_user():
    from werkzeug.security import generate_password_hash

    body = request.get_json(silent=True) or {}
    validate_required(body, "username", "role", "password")

    role = body["role"]
    validate_enum(role, UserRole, "role")

    if store.get_user_by_username(body["username"]):
        raise ValidationError("Username đã tồn tại")

    permissions = body.get("permissions", [])

    api_key = f"{role.lower()}-{str(uuid.uuid4())[:16]}"

    user = User(
        id=str(uuid.uuid4()),
        username=body["username"],
        api_key=api_key,
        password_hash=generate_password_hash(body["password"]),
        role=role,
        permissions=permissions,
        is_active=body.get("is_active", True),
        default_veo_cookie=body.get("default_veo_cookie", None),
        default_proxy_url=body.get("default_proxy_url", None),
    )
    store.create_user(user)

    return success(user.to_public_dict(), 201)


@app.route("/api/admin/users/<user_id>", methods=["PUT"])
@require_api_key
@require_role(UserRole.ADMIN)
def update_user(user_id):
    from werkzeug.security import generate_password_hash

    user = store.get_user(user_id)
    if not user:
        raise VietAutoAPIError("User not found", 404)

    body = request.get_json(silent=True) or {}

    if "role" in body:
        validate_enum(body["role"], UserRole, "role")
        user.role = body["role"]

    if "permissions" in body:
        user.permissions = body["permissions"]

    if "is_active" in body:
        user.is_active = bool(body["is_active"])

    if "password" in body and body["password"].strip():
        user.password_hash = generate_password_hash(body["password"].strip())

    if "default_veo_cookie" in body:
        user.default_veo_cookie = body["default_veo_cookie"]

    if "default_proxy_url" in body:
        user.default_proxy_url = body["default_proxy_url"]

    store.update_user(user)
    return success(user.to_public_dict())


@app.route("/api/admin/users/<user_id>/veo-accounts", methods=["PUT"])
@require_api_key
@require_role(UserRole.ADMIN)
def update_user_veo_accounts(user_id):
    user = store.get_user(user_id)
    if not user:
        raise VietAutoAPIError("User not found", 404)
    body = request.get_json(silent=True) or {}
    account_ids = body.get("account_ids", [])
    if account_ids is None:
        account_ids = []
    if not isinstance(account_ids, list):
        raise ValidationError("account_ids must be a list")

    result = store.set_veo_accounts_for_user(user_id, account_ids)
    assigned_accounts = store.list_veo_accounts_for_user(user_id)
    return success({
        "user_id": user_id,
        "username": user.username,
        "assigned_veo_account_ids": [a.id for a in assigned_accounts],
        "assigned_veo_account_names": [a.name for a in assigned_accounts],
        "assigned": result.get("assigned", 0),
        "unassigned": result.get("unassigned", 0),
    })


@app.route("/api/admin/users/<user_id>", methods=["DELETE"])
@require_api_key
@require_role(UserRole.ADMIN)
def delete_user(user_id):
    if store.delete_user(user_id):
        return success({"deleted": user_id})
    raise VietAutoAPIError("User not found", 404)


@app.route("/api/admin/settings/cookie", methods=["POST"])
@require_api_key
@require_role(UserRole.ADMIN)
def update_cookie():
    body = request.get_json(silent=True) or {}
    cookie_data = body.get("cookie")
    if not cookie_data:
        raise ValidationError("Cookie data is required")

    import json

    cookie_path = os.path.join(ROOT_DIR, "cookies.json")
    try:
        if isinstance(cookie_data, str) and cookie_data.strip().startswith("["):
            cookie_data = json.loads(cookie_data)
        elif isinstance(cookie_data, str) and cookie_data.strip().startswith("{"):
            cookie_data = json.loads(cookie_data)
        elif isinstance(cookie_data, str):
            cookie_data = [
                {"name": "__Secure-next-auth.session-token", "value": cookie_data}
            ]

        with open(cookie_path, "w", encoding="utf-8") as f:
            json.dump(cookie_data, f, indent=2)

        global _veo_service
        _veo_service = None
        get_veo_service()

        return success({"message": "Cookie updated successfully"})
    except Exception as e:
        raise VietAutoAPIError(f"Lỗi định dạng cookie: {e}", 400)


@app.route("/api/veo/queue/settings", methods=["GET"])
def get_queue_settings():
    veo = get_veo_service()
    status = veo.get_queue_status()
    return success(status)


_QUEUE_MONITOR_CACHE = {"data": None, "ts": 0.0}
_QUEUE_MONITOR_CACHE_TTL = 2.0
_QUEUE_MONITOR_CACHE_LOCK = threading.Lock()


@app.route("/api/admin/queue/monitor", methods=["GET"])
def get_queue_monitor():
    import time as _time

    _now_cache = _time.time()
    with _QUEUE_MONITOR_CACHE_LOCK:
        _cached = _QUEUE_MONITOR_CACHE["data"]
        _cached_ts = _QUEUE_MONITOR_CACHE["ts"]
    if _cached is not None and (_now_cache - _cached_ts) < _QUEUE_MONITOR_CACHE_TTL:
        return success(_cached)

    veo = get_veo_service()
    queue_stats = veo.get_queue_status()
    now = _time.time()

    projects = store.list_projects()
    project_map = {p.id: p.name for p in projects if p}

    processing_raw, _ = store.list_video_tasks(status="PROCESSING", limit=200)
    processing_tasks = []
    for t in processing_raw:
        if not t:
            continue
        created = getattr(t, "created_at", 0) or 0
        processing_tasks.append({
            "id": t.id,
            "name": getattr(t, "name", ""),
            "project_id": getattr(t, "project_id", ""),
            "project_name": project_map.get(getattr(t, "project_id", ""), "—"),
            "action_type": getattr(t, "action_type", ""),
            "model": getattr(t, "model", ""),
            "screen_ratio": getattr(t, "screen_ratio", ""),
            "account": getattr(t, "picked_account_name", None) or "—",
            "status": "PROCESSING",
            "created_at": created,
            "running_time_s": round(now - created) if created else 0,
        })
    processing_tasks.sort(key=lambda x: x["running_time_s"], reverse=True)

    pending_raw, _ = store.list_video_tasks(status="PENDING", limit=500)
    pending_tasks = []
    for t in pending_raw:
        if not t:
            continue
        created = getattr(t, "created_at", 0) or 0
        pending_tasks.append({
            "id": t.id,
            "name": getattr(t, "name", ""),
            "project_id": getattr(t, "project_id", ""),
            "project_name": project_map.get(getattr(t, "project_id", ""), "—"),
            "action_type": getattr(t, "action_type", ""),
            "model": getattr(t, "model", ""),
            "screen_ratio": getattr(t, "screen_ratio", ""),
            "status": str(getattr(t, "status", "PENDING")),
            "created_at": created,
            "waiting_time_s": round(now - created) if created else 0,
        })

    project_summary = {}
    for t in processing_tasks + pending_tasks:
        pid = t["project_id"]
        pname = t["project_name"]
        if pid not in project_summary:
            project_summary[pid] = {"id": pid, "name": pname, "processing": 0, "pending": 0}
        if t["status"] == "PROCESSING":
            project_summary[pid]["processing"] += 1
        else:
            project_summary[pid]["pending"] += 1

    _result_payload = {
        "queue_stats": queue_stats,
        "processing_tasks": processing_tasks,
        "processing_count": len(processing_tasks),
        "pending_tasks": pending_tasks,
        "pending_count": len(pending_tasks),
        "project_summary": list(project_summary.values()),
    }
    with _QUEUE_MONITOR_CACHE_LOCK:
        _QUEUE_MONITOR_CACHE["data"] = _result_payload
        _QUEUE_MONITOR_CACHE["ts"] = _time.time()
    return success(_result_payload)


@app.route("/api/admin/logs/tail", methods=["GET"])
@require_api_key
@require_role(UserRole.ADMIN)
def get_logs_tail():
    project_id = (request.args.get("project_id") or "").strip()
    task_id = (request.args.get("task_id") or "").strip()
    account_name = (request.args.get("account_name") or "").strip()
    contains = (request.args.get("contains") or "").strip()
    try:
        lines_limit = int(request.args.get("lines") or 300)
    except Exception:
        lines_limit = 300
    lines_limit = max(10, min(2000, lines_limit))
    include_rotated = str(request.args.get("include_rotated") or "").strip() in ("1", "true", "yes")
    try:
        since_byte = int(request.args.get("since_byte") or 0)
    except Exception:
        since_byte = 0
    since_byte = max(0, since_byte)
    incremental = since_byte > 0

    keywords = [s for s in (project_id, task_id, account_name, contains) if s]

    _LOG_DIR = os.path.join(ROOT_DIR, "logs")
    current_file = os.path.join(_LOG_DIR, "veo_api.log")

    matched_lines = []
    read_files = []
    next_byte = 0
    reset = False  # File đã rotate hoặc truncate → reset cursor

    if incremental:
        if os.path.isfile(current_file):
            try:
                size = os.path.getsize(current_file)
                if size < since_byte:
                    reset = True
                    incremental = False
                else:
                    with open(current_file, "r", encoding="utf-8", errors="replace") as _f:
                        _f.seek(since_byte)
                        new_chunk = _f.read()
                        next_byte = _f.tell()
                    new_lines = new_chunk.splitlines() if new_chunk else []
                    read_files.append({
                        "path": os.path.basename(current_file),
                        "lines_scanned": len(new_lines),
                        "since_byte": since_byte,
                        "next_byte": next_byte,
                    })
                    if keywords:
                        for ln in new_lines:
                            if any(k in ln for k in keywords):
                                matched_lines.append(ln)
                    else:
                        matched_lines.extend(new_lines)
            except Exception as e:
                read_files.append({"path": os.path.basename(current_file), "error": str(e)[:120]})
        else:
            next_byte = 0

    if not incremental:
        TAIL_BYTES_NO_FILTER = 512 * 1024       # 512KB
        TAIL_BYTES_WITH_FILTER = 8 * 1024 * 1024  # 8MB khi có keyword
        _tail_bytes = TAIL_BYTES_WITH_FILTER if keywords else TAIL_BYTES_NO_FILTER

        candidates = [current_file]
        if include_rotated:
            for i in range(1, 6):
                candidates.append(os.path.join(_LOG_DIR, f"veo_api.log.{i}"))
        for path in candidates:
            if not os.path.isfile(path):
                continue
            try:
                _sz = os.path.getsize(path)
                _start = max(0, _sz - _tail_bytes)
                with open(path, "rb") as _fb:
                    _fb.seek(_start)
                    _chunk = _fb.read()
                try:
                    _text = _chunk.decode("utf-8", errors="replace")
                except Exception:
                    _text = _chunk.decode("latin-1", errors="replace")
                _file_lines = _text.splitlines()
                if _start > 0 and _file_lines:
                    _file_lines = _file_lines[1:]
                read_files.append({
                    "path": os.path.basename(path),
                    "lines_scanned": len(_file_lines),
                    "tail_bytes": len(_chunk),
                    "file_size": _sz,
                })
                if keywords:
                    for ln in _file_lines:
                        if any(k in ln for k in keywords):
                            matched_lines.append(ln)
                else:
                    matched_lines.extend(_file_lines)
            except Exception as e:
                read_files.append({"path": os.path.basename(path), "error": str(e)[:120]})
        try:
            next_byte = os.path.getsize(current_file) if os.path.isfile(current_file) else 0
        except Exception:
            next_byte = 0

    result_lines = matched_lines[-lines_limit:] if matched_lines else []

    return success({
        "filters": {
            "project_id": project_id,
            "task_id": task_id,
            "account_name": account_name,
            "contains": contains,
            "keywords": keywords,
        },
        "files_read": read_files,
        "total_matched": len(matched_lines),
        "lines_returned": len(result_lines),
        "lines": result_lines,
        "next_byte": next_byte,
        "incremental": incremental and not reset,
        "reset": reset,
    })


@app.route("/api/veo/queue/settings", methods=["PUT"])
@require_api_key
@require_role(UserRole.ADMIN)
def update_queue_settings():
    body = request.get_json(silent=True) or {}
    n = body.get("max_concurrent")
    if n is None:
        raise ValidationError("Thiếu field 'max_concurrent'")
    try:
        n = int(n)
        if n < 1 or n > 200:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValidationError("max_concurrent phải là số nguyên từ 1 đến 200")

    veo = get_veo_service()
    old_val = getattr(veo, "_max_concurrent", 0)
    veo.update_max_concurrent(n)
    return success({
        "message": f"Đã cập nhật max_concurrent = {n}",
        "max_concurrent": n,
        "old": old_val
    })


@app.route("/api/veo/upload", methods=["POST"])
@require_api_key
def upload_image_standalone():
    body = request.get_json(silent=True) or {}
    validate_required(body, "image_base64")
    
    img_b64 = body["image_base64"]
    project_id = body.get("project_id")
    account_name = body.get("account_name")
    
    veo = get_veo_service()
    from core.imagen_client import ImagenClient
    
    target_account = account_name
    if not target_account:
        accounts = store.list_veo_accounts(status="ACTIVE")
        if not accounts:
            raise VietAutoAPIError("Không có account Veo khả dụng để upload", 500)
        activities = veo.get_account_activities()
        idle_accounts = [a for a in accounts if a.username not in activities]
        target_account = idle_accounts[0].username if idle_accounts else accounts[0].username

    acc_obj = store.get_veo_account(target_account)
    if not acc_obj:
         raise VietAutoAPIError(f"Account {target_account} không tồn tại", 404)
    
    proxy = acc_obj.proxy_url or store.get_setting("default_proxy", "")
    img_client = ImagenClient(acc_obj.cookie, proxy=proxy)
    
    token = acc_obj.access_token
    if not token:
        if not img_client.get_session_token():
             raise VietAutoAPIError("Không thể lấy access_token cho account này", 500)
        token = img_client.access_token
        acc_obj.access_token = token
        store.update_veo_account(acc_obj)
    else:
        img_client.access_token = token
    
    media_id = img_client.upload_user_image(img_b64, project_id=project_id)
    if not media_id:
        raise VietAutoAPIError("Upload lên Google thất bại", 500)
        
    return success({"mediaId": media_id})


@app.route("/api/media/image-blob", methods=["GET"])
@require_api_key
def media_image_blob():
    raw_path = (request.args.get("path") or "").strip()
    if not raw_path:
        raise ValidationError("path is required")

    normalized = os.path.abspath(os.path.normpath(raw_path))
    root_abs = os.path.abspath(ROOT_DIR)
    allowed_roots = [
        root_abs,
        os.path.abspath(os.path.join(ROOT_DIR, "uploads")),
        os.path.abspath(os.path.join(ROOT_DIR, "output")),
        os.path.abspath(os.path.join(ROOT_DIR, "outputs")),
    ]
    if not any(normalized == base or normalized.startswith(base + os.sep) for base in allowed_roots):
        raise VietAutoAPIError("Image path is outside allowed workspace", 403)
    if not os.path.isfile(normalized):
        raise VietAutoAPIError("Image file not found", 404)

    ext = os.path.splitext(normalized)[1].lower()
    mime_by_ext = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
    }
    mimetype = mime_by_ext.get(ext)
    if not mimetype:
        raise VietAutoAPIError("Unsupported image type", 400)
    return send_file(normalized, mimetype=mimetype, as_attachment=False, download_name=os.path.basename(normalized))


@app.route("/api/veo/projects", methods=["GET"])
@require_api_key
def list_projects():
    projects = store.list_projects()
    stats = store.get_all_project_stats()
    image_stats = store.get_all_project_image_stats()  # {project_id: total_images}

    data = []
    for p in projects:
        d = p.to_dict()
        s = stats.get(p.id, {})
        d["completed_count"] = s.get("COMPLETED", 0)
        d["failed_count"] = s.get("FAILED", 0) + s.get("ERROR", 0)
        d["pending_count"] = s.get("PENDING", 0) + s.get("PROCESSING", 0)
        d["paused_count"] = s.get("PAUSED", 0)

        computed_total = d["completed_count"] + d["failed_count"] + d["pending_count"] + d["paused_count"]
        d["total_videos"] = computed_total

        _img_count = image_stats.get(p.id, 0)
        d["completed_images_count"] = max(_img_count, d["completed_count"])

        data.append(d)

    return success(data)


@app.route("/api/veo/project", methods=["POST"])
@require_api_key
def create_project():
    body = request.get_json(silent=True) or {}
    validate_required(body, "name")

    project = Project(
        id=str(uuid.uuid4()),
        name=body["name"].strip(),
    )
    store.create_project(project)
    return success(project.to_dict(), 201)


@app.route("/api/veo/project/batch-create", methods=["POST"])
@require_api_key
def batch_create_project():
    body = request.get_json(silent=True) or {}
    validate_required(body, "name", "prompts")
    prompts = body.get("prompts", [])
    if not isinstance(prompts, list) or not prompts:
        raise ValidationError("Field 'prompts' must be a non-empty list of strings.")

    model = body.get("model", VeoModel.T2V_FAST)
    screen_ratio = body.get("screen_ratio", ScreenRatio.LANDSCAPE)
    videos_per_prompt = safe_int(body.get("videos_per_prompt", 1), 1)
    max_threads = safe_int(body.get("max_threads", 2), 2)
    upsample_resolution = body.get("upsample_resolution", None)  # None | "1K" | "2K" | "4K"
    
    image_refs_raw = body.get("image_refs", [])
    if image_refs_raw and not isinstance(image_refs_raw, list):
        raise ValidationError("Field 'image_refs' must be a list of strings or objects if provided.")

    image_refs = []
    upload_dir = os.path.join(ROOT_DIR, "output", "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    import base64
    import mimetypes
    for ref in image_refs_raw:
        if isinstance(ref, dict) and "b64" in ref:
            try:
                img_data = base64.b64decode(ref["b64"])
                ext = mimetypes.guess_extension(ref.get("mime", "image/jpeg")) or ".jpg"
                filename = f"upload_{uuid.uuid4().hex[:12]}{ext}"
                filepath = os.path.abspath(os.path.join(upload_dir, filename))
                with open(filepath, "wb") as f:
                    f.write(img_data)
                image_refs.append({"path": filepath, "mime": ref.get("mime", "image/jpeg"), "name": ref.get("name", filename)})
            except Exception as e:
                pass
        else:
            image_refs.append(ref)

    banana_access_token = (body.get("access_token") or body.get("google_access_token") or "").strip()
    if banana_access_token.startswith("Bearer "):
        banana_access_token = banana_access_token[7:].strip()
    veo_cookie = body.get("veo_cookie", None)
    proxy_url = body.get("proxy_url", None)


    project_id = body.get("project_id")
    
    total_videos = len(prompts) * videos_per_prompt

    if project_id:
        project = store.get_project(project_id)
        if not project:
            raise VietAutoAPIError("Dự án không tồn tại.", 404)
        
        store._projects.update_one(
            {"id": project.id}, {"$inc": {"total_videos": total_videos}}
        )
    else:
        project = Project(
            id=str(uuid.uuid4()), name=body["name"].strip(), total_videos=total_videos
        )
        store.create_project(project)

    from web.models import ImageModel
    is_image = model in [m.value for m in ImageModel]

    veo = None
    if not is_image:
        veo = get_veo_service()
        if not veo:
            raise VietAutoAPIError("Chua setup Cookie Veo.", 500)


    tasks_to_process = []
    for i, prompt in enumerate(prompts):
        if not isinstance(prompt, str) or not prompt.strip():
            continue

        clean_prompt = prompt.strip()
        for v in range(videos_per_prompt):
            video_name = f"{project.name} - Part {i+1}"
            if videos_per_prompt > 1:
                video_name += f" (Var {v+1})"

            task = VideoTask(
                id=str(uuid.uuid4()),
                project_id=project.id,
                name=video_name,
                action_type=ActionType.CREATE_IMAGE if is_image else ActionType.TEXT_TO_VIDEO,
                model=model,
                screen_ratio=screen_ratio,
                prompts=[clean_prompt],
                status=TaskStatus.PENDING,
                veo_cookie=banana_access_token if is_image else veo_cookie,
                proxy_url=proxy_url,
                image_refs=image_refs
            )
            if is_image and upsample_resolution:
                task.raw_result = {"upsample_resolution": upsample_resolution}
            store.create_video_task(task)
            tasks_to_process.append(task)

    if is_image:
        veo = get_veo_service()
        for task in tasks_to_process:
            veo.create_image(
                project_id=task.project_id,
                name=task.name,
                prompt=(task.prompts or [""])[0],
                model=task.model,
                aspect=task.screen_ratio,
                count=getattr(task, "count", 1) or 1,
                image_refs=task.image_refs,
                veo_cookie=task.veo_cookie,
                proxy_url=task.proxy_url,
                task=task,
            )
        return success(project.to_dict(), 201)


    def _enqueue_all(tasks):
        import time as _time
        for idx, t in enumerate(tasks):
            if idx > 0:
                try:
                    _delay = veo.get_enqueue_delay() if hasattr(veo, "get_enqueue_delay") else 2
                except Exception:
                    _delay = 2
                _time.sleep(_delay)
            if t.action_type == ActionType.CREATE_IMAGE:
                run_fn = veo._run_create_image if hasattr(veo, "_run_create_image") else None
                if run_fn:
                    veo.enqueue(t, run_fn)
                else:
                    pass
            else:
                veo.enqueue(t, veo._run_t2v)

    import threading
    threading.Thread(target=_enqueue_all, args=(tasks_to_process,), daemon=True).start()

    return success(project.to_dict(), 201)


@app.route("/api/veo/project/import-excel", methods=["POST"])
@require_api_key
def import_excel_tasks():
    import base64
    import mimetypes

    body = request.get_json(silent=True) or {}

    tasks_input = body.get("tasks", [])
    if not isinstance(tasks_input, list) or not tasks_input:
        raise ValidationError("Field 'tasks' phải là danh sách không rỗng [{prompt, image_paths}].")

    model = body.get("model", "NARWHAL")
    screen_ratio = body.get("screen_ratio", "IMAGE_ASPECT_RATIO_PORTRAIT")
    max_threads = safe_int(body.get("max_threads", 5), 5)
    upsample_resolution = body.get("upsample_resolution", None)
    banana_access_token = (body.get("access_token") or body.get("google_access_token") or "").strip()
    if banana_access_token.startswith("Bearer "):
        banana_access_token = banana_access_token[7:].strip()

    project_id = body.get("project_id")
    total_tasks = len(tasks_input)

    if project_id:
        project = store.get_project(project_id)
        if not project:
            raise VietAutoAPIError("Dự án không tồn tại.", 404)
        store._projects.update_one(
            {"id": project.id}, {"$inc": {"total_videos": total_tasks}}
        )
    else:
        pname = (body.get("name") or "Excel Import Project").strip()
        project = Project(id=str(uuid.uuid4()), name=pname, total_videos=total_tasks)
        store.create_project(project)

    from web.models import ImageModel
    is_image = model in [m.value for m in ImageModel]

    veo = None
    if not is_image:
        veo = get_veo_service()
        if not veo:
            raise VietAutoAPIError("Chua setup Cookie Veo.", 500)

    def _read_image_refs(image_paths):
        refs = []
        import mimetypes
        import requests
        import uuid
        import re

        upload_dir = os.path.join(ROOT_DIR, "output", "uploads")
        os.makedirs(upload_dir, exist_ok=True)

        for path in (image_paths or []):
            path = path.strip()
            if not path:
                continue
            try:
                if path.startswith("http://") or path.startswith("https://"):
                    file_id = None
                    match_d = re.search(r"/file/d/([a-zA-Z0-9_-]+)", path)
                    if match_d:
                        file_id = match_d.group(1)
                    else:
                        match_id = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", path)
                        if match_id:
                            file_id = match_id.group(1)
                            
                    if file_id:
                        direct_url = f"https://drive.google.com/uc?export=download&id={file_id}"
                        target_url = direct_url
                    else:
                        target_url = path
                    
                    try:
                        session = requests.Session()
                        r = session.get(target_url, timeout=30)
                        r.raise_for_status()

                        content_type = r.headers.get("Content-Type", "")

                        # Nếu GDrive trả về trang xác nhận HTML (virus scan warning) → bypass
                        if "text/html" in content_type and file_id:
                            confirm_match = re.search(r'confirm=([0-9A-Za-z_\-]+)', r.text)
                            confirm_token = confirm_match.group(1) if confirm_match else "t"
                            confirm_url = (
                                f"https://drive.google.com/uc?export=download"
                                f"&id={file_id}&confirm={confirm_token}"
                            )
                            r = session.get(confirm_url, timeout=60)
                            r.raise_for_status()
                            content_type = r.headers.get("Content-Type", "")

                        if "text/html" in content_type:
                            continue

                        ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ".jpg"
                        if ext in (".jpe", ".htm", ".html"):
                            ext = ".jpg"

                        filename = f"excel_dl_{uuid.uuid4().hex[:8]}{ext}"
                        dl_path = os.path.abspath(os.path.join(upload_dir, filename))

                        with open(dl_path, "wb") as f:
                            f.write(r.content)


                        mime, _ = mimetypes.guess_type(dl_path)
                        refs.append({"path": dl_path, "mime": mime or "image/jpeg", "name": os.path.basename(dl_path)})
                        continue
                    except Exception as req_e:
                        continue
                        
                abs_path = os.path.abspath(path)
                if not os.path.isfile(abs_path):
                    continue
                mime, _ = mimetypes.guess_type(abs_path)
                if not mime:
                    mime = "image/jpeg"

                ext = os.path.splitext(abs_path)[1] or ".jpg"
                safe_name = f"upload_{uuid.uuid4().hex[:12]}{ext}"
                safe_path = os.path.join(upload_dir, safe_name)
                import shutil
                shutil.copy2(abs_path, safe_path)

                refs.append({"path": safe_path, "mime": mime, "name": os.path.basename(safe_path)})
            except Exception as e:
                pass
        return refs

    tasks_to_process = []
    for i, t in enumerate(tasks_input):
        if not isinstance(t, dict):
            continue
        prompt = (t.get("prompt") or "").strip()
        if not prompt:
            continue

        image_refs = _read_image_refs(t.get("image_paths", []))

        if is_image:
            act = ActionType.CREATE_IMAGE
        elif image_refs:
            act = ActionType.IMAGE_TO_VIDEO
        else:
            act = ActionType.TEXT_TO_VIDEO

        video_name = f"{project.name} - Row {i + 1}"
        task = VideoTask(
            id=str(uuid.uuid4()),
            project_id=project.id,
            name=video_name,
            action_type=act,
            model=model,
            screen_ratio=screen_ratio,
            prompts=[prompt],
            status=TaskStatus.PENDING,
            image_refs=image_refs,
            veo_cookie=banana_access_token if is_image else None,
        )

        if is_image and upsample_resolution:
            task.raw_result = {"upsample_resolution": upsample_resolution}
        elif act == ActionType.IMAGE_TO_VIDEO and image_refs:
            first_ref = image_refs[0]
            img_path = first_ref.get("path", "") if isinstance(first_ref, dict) else first_ref
            task.raw_result = {"image_path": img_path}
            if len(image_refs) >= 2:
                second_ref = image_refs[1]
                end_path = second_ref.get("path", "") if isinstance(second_ref, dict) else second_ref
                task.raw_result["end_image_path"] = end_path

        store.create_video_task(task)
        tasks_to_process.append(task)


    if is_image:
        veo = get_veo_service()
        for task in tasks_to_process:
            veo.create_image(
                project_id=task.project_id,
                name=task.name,
                prompt=(task.prompts or [""])[0],
                model=task.model,
                aspect=task.screen_ratio,
                count=getattr(task, "count", 1) or 1,
                image_refs=task.image_refs,
                veo_cookie=task.veo_cookie,
                proxy_url=task.proxy_url,
                task=task,
            )
        return success(project.to_dict(), 201)


    _I2V_TYPES_EXCEL = {ActionType.IMAGE_TO_VIDEO, ActionType.IMAGES_TO_VIDEO, ActionType.FRAMES_TO_VIDEO}

    def _enqueue_all_excel(tasks):
        import time as _time

        i2v_tasks = [t for t in tasks if t.action_type in _I2V_TYPES_EXCEL]
        if i2v_tasks:
            try:
                veo._pre_upload_images(i2v_tasks, project.id)
            except Exception as e:
                pass

        for idx, t in enumerate(tasks):
            if idx > 0:
                try:
                    _delay = veo.get_enqueue_delay() if hasattr(veo, "get_enqueue_delay") else 0.5
                    _delay = max(0.5, _delay if _delay > 2 else 0.5)
                except Exception:
                    _delay = 0.5
                _time.sleep(_delay)
            if t.action_type == ActionType.CREATE_IMAGE:
                run_fn = getattr(veo, "_run_create_image", None)
                if run_fn:
                    veo.enqueue(t, run_fn)
                else:
                    pass
            elif t.action_type in _I2V_TYPES_EXCEL:
                run_fn = getattr(veo, "_run_i2v", None)
                if run_fn:
                    veo.enqueue(t, run_fn)
                else:
                    pass
            else:
                veo.enqueue(t, veo._run_t2v)

    import threading as _threading
    _threading.Thread(target=_enqueue_all_excel, args=(tasks_to_process,), daemon=True).start()

    return success(project.to_dict(), 201)


@app.route("/api/veo/project/<project_id>/retry-all", methods=["POST"])
@require_api_key
def retry_all_project(project_id):
    project = store.get_project(project_id)
    if not project:
        raise VietAutoAPIError("Dự án không tồn tại", 404)

    veo = get_veo_service()
    import time as _time

    now_ts = _time.time()
    reset_fields = {
        "status": "PENDING",
        "error": None,
        "media_id": None,
        "media_url": None,
        "output_filename": None,
        "completed_at": None,
        "veo_cookie": None,
        "picked_account_name": None,
        "updated_at": now_ts,
        "created_at": now_ts,
    }
    unset_fields = {
        "raw_result.video_path": "",
        "raw_result.video_url": "",
        "raw_result.download_url": "",
        "raw_result.media_id": "",
        "raw_result.operation_id": "",
        "raw_result.error": "",
    }

    try:
        bulk_result = store._video_tasks.update_many(
            {"project_id": project_id},
            {"$set": reset_fields, "$unset": unset_fields}
        )
        retried_count = bulk_result.modified_count
    except Exception:
        all_tasks, _ = store.list_video_tasks(project_id=project_id, skip=0, limit=10000)
        retried_count = len(all_tasks)
        for task in all_tasks:
            task.status = "PENDING"
            task.error = None
            task.media_id = None
            task.media_url = None
            task.output_filename = None
            task.completed_at = None
            task.veo_cookie = None
            task.picked_account_name = None
            task.created_at = now_ts
            task.updated_at = now_ts
            raw = task.raw_result or {}
            for key in ("video_path", "video_url", "download_url", "media_id", "operation_id", "error"):
                raw.pop(key, None)
            task.raw_result = raw
            store.update_video_task(task)

    if retried_count == 0:
        return success({"retried": 0, "message": "Project không có task để retry"})

    def _enqueue_all_reset():
        _I2V_TYPES = {ActionType.IMAGE_TO_VIDEO, ActionType.IMAGES_TO_VIDEO, ActionType.FRAMES_TO_VIDEO}
        tasks_pending, _ = store.list_video_tasks(project_id=project_id, status="PENDING", skip=0, limit=10000)
        i2v_tasks_retry = [t for t in tasks_pending if getattr(t, "action_type", None) in _I2V_TYPES]
        if i2v_tasks_retry:
            try:
                veo._pre_upload_images(i2v_tasks_retry, project_id)
            except Exception:
                pass

        for task in tasks_pending:
            at = getattr(task, "action_type", None)
            if at == ActionType.CREATE_IMAGE:
                run_fn = getattr(veo, "_run_create_image", None)
                if not run_fn:
                    continue
            elif at in _I2V_TYPES:
                run_fn = getattr(veo, "_run_i2v", None)
                if not run_fn:
                    continue
                raw = task.raw_result or {}
                img_path = raw.get("image_path")
                if img_path and not raw.get("images_b64") and not os.path.exists(img_path):
                    task.status = TaskStatus.FAILED.value if hasattr(TaskStatus.FAILED, 'value') else 'FAILED'
                    task.error = f"Ảnh gốc không còn: {img_path}. Vui lòng tạo lại task I2V mới."
                    store.update_video_task(task)
                    continue
            else:
                run_fn = veo._run_t2v
            veo.enqueue(task, run_fn)

    import threading
    threading.Thread(target=_enqueue_all_reset, daemon=True).start()

    return success({
        "retried": retried_count,
        "message": f"Đã reset {retried_count} task về mới tinh và đưa vào hàng chờ chạy lại",
    })


@app.route("/api/veo/project/<project_id>/retry-failed", methods=["POST"])
@require_api_key
def retry_failed_project(project_id):
    project = store.get_project(project_id)
    if not project:
        raise VietAutoAPIError("Dự án không tồn tại", 404)

    veo = get_veo_service()
    import time as _time

    _statuses_to_retry = ["FAILED", "ERROR", "PENDING", "PROCESSING"]
    try:
        bulk_result = store._video_tasks.update_many(
            {
                "project_id": project_id,
                "status": {"$in": _statuses_to_retry},
                "$or": [
                    {"error": None},
                    {"error": {"$not": {"$regex": "400"}}},
                ]
            },
            {
                "$set": {
                    "status": "PENDING",
                    "error": None,
                    "veo_cookie": None,
                    "picked_account_name": None,
                    "updated_at": _time.time(),
                    "created_at": _time.time(),
                }
            }
        )
        retried_count = bulk_result.modified_count
    except Exception as e:
        all_tasks = []
        for st in _statuses_to_retry:
            tasks, _ = store.list_video_tasks(project_id=project_id, status=st, skip=0, limit=10000)
            all_tasks.extend(tasks)
        all_tasks = [t for t in all_tasks if not (t.error and "400" in str(t.error))]
        retried_count = len(all_tasks)
        for task in all_tasks:
            task.status = "PENDING"
            task.error = None
            task.veo_cookie = None
            task.picked_account_name = None
            task.created_at = _time.time()
            store.update_video_task(task)

    if retried_count == 0:
        return success({"retried": 0, "message": "Không có task nào cần retry"})

    def _enqueue_all():
        import time as _time
        _BATCH_SIZE = 10   # Mỗi đợt enqueue 10 tasks
        _BATCH_DELAY = 15  # Nghỉ 15s giữa các đợt
        _TASK_DELAY = 3    # Cách 3s giữa mỗi task trong đợt
        _I2V_TYPES = {ActionType.IMAGE_TO_VIDEO, ActionType.IMAGES_TO_VIDEO, ActionType.FRAMES_TO_VIDEO}
        tasks_pending, _ = store.list_video_tasks(
            project_id=project_id, status="PENDING", skip=0, limit=10000
        )
        _total = len(tasks_pending)

        i2v_tasks_retry = [t for t in tasks_pending if getattr(t, "action_type", None) in _I2V_TYPES]
        if i2v_tasks_retry:
            try:
                veo._pre_upload_images(i2v_tasks_retry, project_id)
            except Exception as _pre_err:
                pass

        for idx, task in enumerate(tasks_pending):
            if idx > 0:
                try:
                    _dyn = veo.get_enqueue_delay() if hasattr(veo, "get_enqueue_delay") else _TASK_DELAY
                except Exception:
                    _dyn = _TASK_DELAY
                _time.sleep(max(_TASK_DELAY, _dyn))
            if idx > 0 and idx % _BATCH_SIZE == 0:
                _time.sleep(_BATCH_DELAY)
            at = getattr(task, "action_type", None)
            if at == ActionType.CREATE_IMAGE:
                run_fn = getattr(veo, "_run_create_image", None)
                if not run_fn:
                    continue
            elif at in _I2V_TYPES:
                run_fn = getattr(veo, "_run_i2v", None)
                if not run_fn:
                    continue
                raw = task.raw_result or {}
                img_path = raw.get("image_path")
                if img_path and not raw.get("images_b64") and not os.path.exists(img_path):
                    task.status = TaskStatus.FAILED.value if hasattr(TaskStatus.FAILED, 'value') else 'FAILED'
                    task.error = f"Ảnh gốc không còn: {img_path}. Vui lòng tạo lại task I2V mới."
                    store.update_video_task(task)
                    continue
            else:
                run_fn = veo._run_t2v
            veo.enqueue(task, run_fn)

    import threading
    threading.Thread(target=_enqueue_all, daemon=True).start()

    return success({
        "retried": retried_count,
        "message": f"Đã đưa {retried_count} task vào hàng chờ thử lại",
    })


@app.route("/api/veo/project/<project_id>/retry-failed-today", methods=["POST"])
@require_api_key
def retry_failed_today(project_id):
    project = store.get_project(project_id)
    if not project:
        raise VietAutoAPIError("Dự án không tồn tại", 404)

    veo = get_veo_service()
    import time as _time
    import datetime as _dt

    _today_start = _dt.datetime.combine(_dt.date.today(), _dt.time.min).timestamp()

    _statuses_to_retry = ["FAILED", "ERROR", "PENDING", "PROCESSING"]
    try:
        bulk_result = store._video_tasks.update_many(
            {
                "project_id": project_id,
                "status": {"$in": _statuses_to_retry},
                "$or": [
                    {"updated_at": {"$gte": _today_start}},
                    {"created_at": {"$gte": _today_start}},
                ],
                "$and": [
                    {"$or": [
                        {"error": None},
                        {"error": {"$not": {"$regex": "400"}}},
                    ]}
                ],
            },
            {
                "$set": {
                    "status": "PENDING",
                    "error": None,
                    "veo_cookie": None,
                    "picked_account_name": None,
                    "updated_at": _time.time(),
                    "created_at": _time.time(),
                }
            }
        )
        retried_count = bulk_result.modified_count
    except Exception as e:
        all_tasks = []
        for st in _statuses_to_retry:
            tasks, _ = store.list_video_tasks(project_id=project_id, status=st, skip=0, limit=10000)
            all_tasks.extend(tasks)
        all_tasks = [
            t for t in all_tasks
            if (getattr(t, "updated_at", 0) >= _today_start or getattr(t, "created_at", 0) >= _today_start)
            and not (t.error and "400" in str(t.error))
        ]
        retried_count = len(all_tasks)
        for task in all_tasks:
            task.status = "PENDING"
            task.error = None
            task.veo_cookie = None
            task.picked_account_name = None
            task.created_at = _time.time()
            store.update_video_task(task)

    if retried_count == 0:
        return success({"retried": 0, "message": "Không có task nào trong hôm nay cần retry"})

    def _enqueue_all_today():
        import time as _time2
        _BATCH_SIZE = 10
        _BATCH_DELAY = 15
        _TASK_DELAY = 3
        _I2V_TYPES = {ActionType.IMAGE_TO_VIDEO, ActionType.IMAGES_TO_VIDEO, ActionType.FRAMES_TO_VIDEO}
        tasks_pending, _ = store.list_video_tasks(
            project_id=project_id, status="PENDING", skip=0, limit=10000
        )
        tasks_pending = [
            t for t in tasks_pending
            if getattr(t, "updated_at", 0) >= _today_start or getattr(t, "created_at", 0) >= _today_start
        ]
        _total = len(tasks_pending)

        i2v_tasks_today = [t for t in tasks_pending if getattr(t, "action_type", None) in _I2V_TYPES]
        if i2v_tasks_today:
            try:
                veo._pre_upload_images(i2v_tasks_today, project_id)
            except Exception as _pre_err:
                pass

        for idx, task in enumerate(tasks_pending):
            if idx > 0:
                try:
                    _dyn = veo.get_enqueue_delay() if hasattr(veo, "get_enqueue_delay") else _TASK_DELAY
                except Exception:
                    _dyn = _TASK_DELAY
                _time2.sleep(max(_TASK_DELAY, _dyn))
            if idx > 0 and idx % _BATCH_SIZE == 0:
                _time2.sleep(_BATCH_DELAY)
            at = getattr(task, "action_type", None)
            if at == ActionType.CREATE_IMAGE:
                run_fn = getattr(veo, "_run_create_image", None)
                if not run_fn:
                    continue
            elif at in _I2V_TYPES:
                run_fn = getattr(veo, "_run_i2v", None)
                if not run_fn:
                    continue
                raw = task.raw_result or {}
                img_path = raw.get("image_path")
                if img_path and not raw.get("images_b64") and not os.path.exists(img_path):
                    task.status = TaskStatus.FAILED.value if hasattr(TaskStatus.FAILED, 'value') else 'FAILED'
                    task.error = f"Ảnh gốc không còn: {img_path}. Vui lòng tạo lại task I2V mới."
                    store.update_video_task(task)
                    continue
            else:
                run_fn = veo._run_t2v
            veo.enqueue(task, run_fn)

    import threading
    threading.Thread(target=_enqueue_all_today, daemon=True).start()

    _today_str = _dt.date.today().strftime("%d/%m/%Y")
    return success({
        "retried": retried_count,
        "message": f"Đã đưa {retried_count} task (ngày {_today_str}) vào hàng chờ thử lại",
    })


@app.route("/api/veo/project/<project_id>/resume-paused", methods=["POST"])
@require_api_key
def resume_paused_project(project_id):
    project = store.get_project(project_id)
    if not project:
        raise VietAutoAPIError("Dự án không tồn tại", 404)

    veo = get_veo_service()
    import time as _time

    try:
        bulk_result = store._video_tasks.update_many(
            {"project_id": project_id, "status": "PAUSED"},
            {"$set": {"status": "PENDING", "error": None, "veo_cookie": None, "picked_account_name": None, "updated_at": _time.time()}}
        )
        resumed_count = bulk_result.modified_count
    except Exception as e:
        tasks, _ = store.list_video_tasks(project_id=project_id, status="PAUSED", skip=0, limit=10000)
        resumed_count = len(tasks)
        for task in tasks:
            task.status = "PENDING"
            task.error = None
            task.veo_cookie = None
            task.picked_account_name = None
            store.update_video_task(task)

    if resumed_count == 0:
        return success({"resumed": 0, "message": "Không có task PAUSED nào"})

    def _enqueue_paused():
        import time as _time
        _BATCH_SIZE = 10
        _BATCH_DELAY = 15
        _TASK_DELAY = 3
        _I2V_TYPES = {ActionType.IMAGE_TO_VIDEO, ActionType.IMAGES_TO_VIDEO, ActionType.FRAMES_TO_VIDEO}
        tasks_pending, _ = store.list_video_tasks(project_id=project_id, status="PENDING", skip=0, limit=10000)
        _total = len(tasks_pending)

        i2v_tasks_resume = [t for t in tasks_pending if getattr(t, "action_type", None) in _I2V_TYPES]
        if i2v_tasks_resume:
            try:
                veo._pre_upload_images(i2v_tasks_resume, project_id)
            except Exception as _pre_err:
                pass

        for idx, task in enumerate(tasks_pending):
            if idx > 0:
                _time.sleep(_TASK_DELAY)
            if idx > 0 and idx % _BATCH_SIZE == 0:
                _time.sleep(_BATCH_DELAY)
            at = getattr(task, "action_type", None)
            if at == ActionType.CREATE_IMAGE:
                run_fn = getattr(veo, "_run_create_image", None)
                if not run_fn: continue
            elif at in _I2V_TYPES:
                run_fn = getattr(veo, "_run_i2v", None)
                if not run_fn: continue
                raw = task.raw_result or {}
                img_path = raw.get("image_path")
                if img_path and not raw.get("images_b64") and not os.path.exists(img_path):
                    task.status = TaskStatus.FAILED.value if hasattr(TaskStatus.FAILED, 'value') else 'FAILED'
                    task.error = f"Ảnh gốc không còn: {img_path}. Vui lòng tạo lại task I2V mới."
                    store.update_video_task(task)
                    continue
            else:
                run_fn = veo._run_t2v
            veo.enqueue(task, run_fn)

    import threading
    threading.Thread(target=_enqueue_paused, daemon=True).start()

    return success({
        "resumed": resumed_count,
        "message": f"Đã resume {resumed_count} task PAUSED vào hàng chờ",
    })

@app.route("/api/veo/project/<project_id>/pause-pending", methods=["POST"])
@require_api_key
@require_role(UserRole.ADMIN)
def pause_pending_project(project_id):
    project = store.get_project(project_id)
    if not project:
        raise VietAutoAPIError("Dự án không tồn tại", 404)

    try:
        import time as _time
        bulk_result = store._video_tasks.update_many(
            {"project_id": project_id, "status": {"$in": ["PENDING", "PROCESSING"]}},
            {"$set": {"status": "PAUSED", "updated_at": _time.time()}}
        )
        modified = bulk_result.modified_count
    except Exception as e:
        tasks_p, _ = store.list_video_tasks(project_id=project_id, status="PENDING", skip=0, limit=10000)
        tasks_r, _ = store.list_video_tasks(project_id=project_id, status="PROCESSING", skip=0, limit=10000)
        modified = 0
        for t in (tasks_p + tasks_r):
            t.status = "PAUSED"
            t.updated_at = _time.time()
            store.update_video_task(t)
            modified += 1

    return success({"paused": modified, "message": f"Đã tạm dừng {modified} task"})


def _media_public_project(prefix: str = "Media API", *, total_videos: int = 1, project_id: str | None = None, project_name: str | None = None) -> Project:
    project_id = (project_id or "").strip()
    if project_id and project_id.upper() != "NEW":
        existing = store.get_project(project_id)
        if existing:
            if total_videos and total_videos > getattr(existing, "total_videos", 0):
                existing.total_videos = max(getattr(existing, "total_videos", 0) or 0, total_videos)
                store.update_project(existing)
            return existing
    name = (project_name or "").strip() or f"{prefix} - {time.strftime('%Y%m%d %H%M%S')}"
    project = Project(id=str(uuid.uuid4()), name=name, total_videos=max(1, int(total_videos or 1)))
    store.create_project(project)
    return project


def _media_prompt_lines(payload: dict) -> list[str]:
    prompts = payload.get("prompts")
    if prompts is None:
        prompts = payload.get("prompt") or ""
    if isinstance(prompts, str):
        lines = prompts.replace("\r\n", "\n").split("\n")
    elif isinstance(prompts, (list, tuple)):
        lines = prompts
    else:
        lines = [str(prompts)]
    return [str(line).strip() for line in lines if str(line).strip()]


def _media_reference_user():
    current = getattr(g, "user", None)
    if current and getattr(current, "is_active", False):
        return current
    users = [u for u in store.list_users() if getattr(u, "is_active", False)]
    admins = [u for u in users if str(getattr(u, "role", "")).lower() == "admin"]
    if admins:
        return admins[0]
    if users:
        return users[0]
    raise AuthenticationError("No active user available for reference-implementation session")


def _media_collect_payload():
    if request.content_type and "multipart/form-data" in request.content_type:
        payload = request.form.to_dict(flat=True)
        image_paths = []
        upload_dir = os.path.join(ROOT_DIR, "output", "uploads")
        os.makedirs(upload_dir, exist_ok=True)
        files = []
        for field in ("file", "files", "files[]", "images", "images[]"):
            files.extend(request.files.getlist(field) or [])
        for f in files:
            if f and f.filename:
                ext = os.path.splitext(f.filename)[1] or ".jpg"
                filepath = os.path.abspath(os.path.join(upload_dir, f"media_api_{uuid.uuid4().hex[:12]}{ext}"))
                f.save(filepath)
                image_paths.append(filepath)
        if image_paths:
            payload["image_paths"] = image_paths
        if "prompts" in payload and isinstance(payload["prompts"], str):
            try:
                parsed_prompts = json.loads(payload["prompts"])
                if isinstance(parsed_prompts, list):
                    payload["prompts"] = parsed_prompts
            except Exception:
                pass
        for key in ("runtime_tokens", "runtime_proxies"):
            if key in payload and isinstance(payload[key], str):
                try:
                    parsed = json.loads(payload[key])
                    if isinstance(parsed, list):
                        payload[key] = parsed
                except Exception:
                    payload[key] = payload[key].replace("\r\n", "\n").split("\n")
        return payload
    payload = request.get_json(silent=True) or {}
    if isinstance(payload, dict):
        for key in ("runtime_tokens", "runtime_proxies"):
            if key in payload and isinstance(payload[key], str):
                payload[key] = payload[key].replace("\r\n", "\n").split("\n")
    return payload


@app.route("/api/veo/media/upload-reference", methods=["POST"])
def media_upload_reference_image():
    file = request.files.get("file") or request.files.get("image")
    if not file or not file.filename:
        raise ValidationError("missing image file")
    ext = os.path.splitext(file.filename)[1].lower() or ".jpg"
    allowed_exts = {".jpg", ".jpeg", ".png", ".webp"}
    if ext not in allowed_exts:
        raise ValidationError("unsupported image type; use jpg, jpeg, png or webp")
    upload_dir = os.path.abspath(os.path.join(ROOT_DIR, "output", "media_api"))
    os.makedirs(upload_dir, exist_ok=True)
    filename = f"upload_{uuid.uuid4().hex[:12]}{ext}"
    filepath = os.path.abspath(os.path.join(upload_dir, filename))
    file.save(filepath)
    return jsonify({
        "success": True,
        "data": {
            "path": filepath.replace("\\", "/"),
            "filename": filename,
            "original_name": file.filename,
        }
    })


def _media_normalize_paths(payload: dict) -> list[str]:
    paths = payload.get("image_paths") or payload.get("images") or []
    if isinstance(paths, (str, dict)):
        paths = [paths]
    result = []
    for item in paths:
        if isinstance(item, dict):
            value = item.get("path") or item.get("local_path") or item.get("file_path") or item.get("url")
        else:
            value = item
        if value:
            result.append(str(value).strip())
    return [p for p in result if p]


def _media_run_reference_task(task: VideoTask, *, user, mode: str, image_paths=None, end_image_paths=None, video_type="single", image_resolution="1K"):
    def runner():
        try:
            task.status = TaskStatus.PROCESSING
            store.update_video_task(task)
            from web.account_session_api import account_session_api
            from core.reference_runtime import run_reference_jobs
            session = account_session_api.issue_for_user(user)
            token = (session.get("token") or "").strip()
            dynamic_project_id = (session.get("project_id") or "").strip()
            if not token or not dynamic_project_id:
                raise RuntimeError("account-session did not return token/project_id")

            def refresh(old_token: str, reason: str) -> str | None:
                fresh = account_session_api.issue_for_user(user, force_refresh=True)
                return (fresh.get("token") or "").strip() or None

            current_proxy_by_token = {token: ""}

            def rotate_proxy(old_token: str, reason: str) -> str | None:
                token_key = _media_token_gate_key(old_token)
                current_proxy = (current_proxy_by_token.get(old_token) or "").strip()
                try:
                    from core.static_proxy_pool import get_fresh_proxy, mark_proxy_dead
                    if current_proxy:
                        mark_proxy_dead(current_proxy, ttl=120)
                    proxy = get_fresh_proxy(exclude={current_proxy} if current_proxy else set())
                except Exception as exc:
                    logger.warning("[MediaAPI] proxy_rotate_on_500 failed token=%s error=%s", token_key, exc)
                    return None
                if proxy:
                    current_proxy_by_token[old_token] = proxy
                    logger.warning(
                        "[MediaAPI] proxy_rotate_on_500 rotated token=%s old_proxy=%s new_proxy=%s source=proxy.txt reason=%s",
                        token_key,
                        current_proxy or "direct",
                        proxy,
                        reason,
                    )
                    return proxy
                logger.warning("[MediaAPI] proxy_rotate_on_500 cooldown_or_empty token=%s old_proxy=%s", token_key, current_proxy or "direct")
                return None

            results = run_reference_jobs(
                token=token,
                project_id=dynamic_project_id,
                prompts=task.prompts,
                mode=mode,
                model=("GEM_PIX_2" if task.model in ("NARWHAL", "GEMINI", "GEM_PIX_2") else task.model),
                aspect_ratio=task.screen_ratio,
                thread_count=1,
                max_attempts=5,
                output_dir=os.path.join(ROOT_DIR, "output", "media_api"),
                reference_paths=image_paths or [],
                end_reference_paths=end_image_paths or [],
                video_type=video_type,
                image_resolution=image_resolution,
                output_prefix=task.id,
                refresh_token_callback=refresh,
                rotate_proxy_callback=rotate_proxy,
            )
            ok = any(r.get("status") == "completed" for r in results)
            first_ok = next((r for r in results if r.get("status") == "completed"), results[0] if results else {})
            task.status = TaskStatus.COMPLETED if ok else TaskStatus.FAILED
            task.media_id = first_ok.get("download_url")
            task.output_filename = first_ok.get("saved_path")
            task.raw_result = {"engine": "reference-implementation", "account_name": session.get("account_name"), "project_id": dynamic_project_id, "results": results}
            if not ok:
                task.error = (first_ok or {}).get("error") or "Task failed"
            store.update_video_task(task)
        except Exception as exc:
            logger.exception(f"[MediaAPI] Task {task.id} failed")
            task.status = TaskStatus.FAILED
            task.error = str(exc)
            task.raw_result = {"engine": "reference-implementation", "error": str(exc)}
            store.update_video_task(task)
    threading.Thread(target=runner, daemon=True).start()


_MEDIA_TOKEN_GATE_LOCK = threading.RLock()
_MEDIA_TOKEN_LOCKS: dict[str, threading.Lock] = {}


def _media_token_gate_key(token: str) -> str:
    return hashlib.sha1(str(token or "").encode("utf-8", errors="ignore")).hexdigest()[:12]


@contextmanager
def _media_acquire_token_chrome_locks(tokens: list[str]):
    clean_tokens = [str(token or "").strip() for token in (tokens or []) if str(token or "").strip()]
    token_keys = sorted({_media_token_gate_key(token) for token in clean_tokens})
    acquired: list[tuple[str, threading.Lock]] = []
    wait_started = time.time()
    if token_keys:
        logger.info(
            "[MediaAPI] token-gate waiting tokens=%s keys=%s reason=single_chrome_per_token_global",
            len(token_keys),
            ",".join(token_keys),
        )
    try:
        for key in token_keys:
            with _MEDIA_TOKEN_GATE_LOCK:
                lock = _MEDIA_TOKEN_LOCKS.get(key)
                if lock is None:
                    lock = threading.Lock()
                    _MEDIA_TOKEN_LOCKS[key] = lock
            lock.acquire()
            acquired.append((key, lock))
        if token_keys:
            logger.info(
                "[MediaAPI] token-gate acquired tokens=%s keys=%s wait_seconds=%.2f single_chrome_per_token_global=1",
                len(token_keys),
                ",".join(token_keys),
                time.time() - wait_started,
            )
        yield
    finally:
        for key, lock in reversed(acquired):
            try:
                lock.release()
            except RuntimeError:
                logger.warning("[MediaAPI] token-gate release skipped key=%s", key)
        if acquired:
            logger.info(
                "[MediaAPI] token-gate released tokens=%s keys=%s single_chrome_per_token_global=1",
                len(acquired),
                ",".join(key for key, _ in acquired),
            )




def _reference_token_fingerprint(token: str, length: int = 12) -> str:
    return hashlib.sha1(str(token or "").encode("utf-8", errors="ignore")).hexdigest()[:length]


def _normalize_reference_proxy(proxy: str | None) -> str:
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


def _reference_check_token_credit(token: str, proxy: str = "", project_id: str = "", timeout: int = 20) -> dict:
    """Check token bằng credits API giống reference/tool mẫu.

    Helper này chỉ dùng cho reference flow mới: token fail sẽ bị lọc trước khi
    vào BananaScheduler, nên không mở Chrome cho token không dùng được.
    """
    token = str(token or "").strip()
    proxy = str(proxy or "").strip()
    project_id = str(project_id or "").strip()
    token_key = _reference_token_fingerprint(token)
    result = {
        "ok": False,
        "token": token,
        "token_fingerprint": token_key,
        "project_id": project_id,
        "proxy": proxy,
        "credits": None,
        "tier": "",
        "error": "",
    }
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
        opener = urllib.request.build_opener()
        normalized_proxy = _normalize_reference_proxy(proxy)
        if normalized_proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": normalized_proxy, "https": normalized_proxy})
            )
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


def _reference_filter_passed_tokens(tokens: list[str], proxies: list[str] | None = None, token_project_map: dict[str, str] | None = None, *, log_prefix: str = "[ReferenceImplementation]") -> dict:
    proxies = proxies or []
    token_project_map = token_project_map or {}
    passed_tokens: list[str] = []
    passed_proxies: list[str] = []
    passed_project_map: dict[str, str] = {}
    checks: list[dict] = []
    for index, token_value in enumerate([str(t).strip() for t in (tokens or []) if str(t).strip()]):
        proxy_value = proxies[index] if index < len(proxies) else ""
        project_id = token_project_map.get(token_value, "")
        info = _reference_check_token_credit(token_value, proxy_value, project_id)
        checks.append(info)
        if info.get("ok"):
            passed_tokens.append(token_value)
            passed_proxies.append(proxy_value)
            if project_id:
                passed_project_map[token_value] = project_id
            logger.info(
                "%s credit-check token=%s ok credits=%s tier=%s proxy=%s",
                log_prefix,
                info.get("token_fingerprint"),
                info.get("credits"),
                info.get("tier"),
                proxy_value or "direct",
            )
        else:
            logger.warning(
                "%s credit-check skip token=%s error=%s proxy=%s",
                log_prefix,
                info.get("token_fingerprint"),
                info.get("error"),
                proxy_value or "direct",
            )
    return {
        "tokens": passed_tokens,
        "proxies": passed_proxies,
        "token_project_map": passed_project_map,
        "checks": checks,
        "passed": len(passed_tokens),
        "failed": len(checks) - len(passed_tokens),
        "total": len(checks),
    }


@app.route("/api/reference/tokens/check-credits", methods=["POST"])
@require_api_key
def reference_tokens_check_credits():
    payload = request.get_json(silent=True) or {}
    tokens = [str(t).strip() for t in (payload.get("tokens") or []) if str(t).strip()]
    proxies = [str(p).strip() for p in (payload.get("proxies") or [])]
    project_map = payload.get("project_map") or payload.get("token_project_map") or {}
    if not isinstance(project_map, dict):
        project_map = {}
    only_pass = bool(payload.get("only_pass", True))
    filtered = _reference_filter_passed_tokens(tokens, proxies, project_map, log_prefix="[ReferenceTokenAPI]")
    response_tokens = [item for item in filtered["checks"] if item.get("ok")] if only_pass else filtered["checks"]
    safe_tokens = []
    for item in response_tokens:
        clean = dict(item)
        clean.pop("token", None)
        safe_tokens.append(clean)
    return jsonify({
        "success": True,
        "data": {
            "total": filtered["total"],
            "passed": filtered["passed"],
            "failed": filtered["failed"],
            "tokens": safe_tokens,
        },
    })


def _media_run_reference_batch(tasks: list[VideoTask], *, user, mode: str, image_paths=None, end_image_paths=None, video_type="single", image_resolution="1K", thread_count=10, runtime_tokens=None, runtime_proxies=None):
    def runner():
        by_prompt: dict[str, list[VideoTask]] = {}
        for task in tasks:
            task.status = TaskStatus.PROCESSING
            store.update_video_task(task)
            by_prompt.setdefault((task.prompts or [""])[0], []).append(task)
        try:
            from web.account_session_api import account_session_api
            from core.reference_runtime import run_reference_jobs
            explicit_tokens = [str(t).strip() for t in (runtime_tokens or []) if str(t).strip()]
            explicit_proxies = [str(p).strip() for p in (runtime_proxies or []) if str(p).strip()]
            if not explicit_proxies:
                try:
                    from core.static_proxy_pool import get_all_proxies
                    explicit_proxies = get_all_proxies()
                except Exception as proxy_exc:
                    logger.warning("[MediaAPI] proxy.txt load failed: %s", proxy_exc)
                    explicit_proxies = []
            account_by_token = {}
            if explicit_tokens:
                if not explicit_proxies:
                    raise RuntimeError("proxy.txt is required for create-job runtime tokens")
                pair_count = min(len(explicit_tokens), len(explicit_proxies))
                tokens = explicit_tokens[:pair_count]
                proxies = explicit_proxies[:pair_count]
                proxy_by_token = {
                    token_value: (proxies[index] if index < len(proxies) else "")
                    for index, token_value in enumerate(tokens)
                }
                if not tokens:
                    raise RuntimeError("No valid token/proxy pair")
                token_project_map = {}
                token_by_account = {}
                primary = {"token": tokens[0], "project_id": "", "account_name": "runtime-token-1"}
                token = tokens[0]
                dynamic_project_id = ""
            else:
                try:
                    deleted_account_names = set(store.list_deleted_accounts())
                except Exception:
                    deleted_account_names = set()
                active_accounts = [
                    a for a in store.list_veo_accounts()
                    if getattr(a, "is_active", False)
                    and (getattr(a, "cookie", None) or "").strip()
                    and getattr(a, "name", "") not in deleted_account_names
                ]
                if deleted_account_names:
                    logger.info(
                        "[MediaAPI] deleted-account blacklist active=%s skipped=%s",
                        len(active_accounts),
                        sorted(deleted_account_names),
                    )
                if explicit_proxies:
                    active_accounts = active_accounts[:len(explicit_proxies)]
                if not active_accounts:
                    raise RuntimeError("No active Veo account with cookie is available")
                sessions = []
                for account in active_accounts:
                    try:
                        sessions.append(account_session_api.issue_for_account_name(getattr(account, "name", ""), user=user, force_refresh=True))
                    except Exception as exc:
                        logger.warning("[MediaAPI] skip account=%s issue_failed=%s", getattr(account, "name", ""), exc)
                if not sessions:
                    raise RuntimeError("No Veo account can issue token/project_id")
                token_by_account = {s.get("account_name"): s for s in sessions}
                account_by_token = {s.get("token", ""): s.get("account_name", "") for s in sessions if s.get("token") and s.get("account_name")}
                selected_accounts = [a for a in active_accounts if getattr(a, "name", "") in token_by_account]
                tokens = [(token_by_account.get(getattr(a, "name", "")) or {}).get("token", "") for a in selected_accounts]
                if explicit_proxies:
                    pair_count = min(len(tokens), len(explicit_proxies))
                    tokens = tokens[:pair_count]
                    selected_accounts = selected_accounts[:pair_count]
                    proxies = explicit_proxies[:pair_count]
                else:
                    proxies = [""] * len(tokens)
                proxy_by_token = {
                    token_value: (proxies[index] if index < len(proxies) else "")
                    for index, token_value in enumerate(tokens)
                }
                token_project_map = {s.get("token", ""): s.get("project_id", "") for s in sessions if s.get("token") and s.get("project_id")}
                primary = token_by_account.get(getattr(selected_accounts[0], "name", ""), sessions[0]) if selected_accounts else sessions[0]
                token = (primary.get("token") or "").strip()
                dynamic_project_id = (primary.get("project_id") or "").strip()
            media_per_token_threads = 3
            logger.info(
                "[MediaAPI] create-job tokens=%s lanes=%s proxies=%s chrome=dedicated single_chrome_per_token=1 per_token_threads=%s source=%s",
                len(tokens),
                len(tokens) * media_per_token_threads,
                len([p for p in proxies if p]),
                media_per_token_threads,
                "runtime_proxies" if runtime_proxies else "proxy.txt",
            )
            if not token:
                raise RuntimeError("runtime token is required")
            if not explicit_tokens and not dynamic_project_id:
                raise RuntimeError("account-session did not return project_id")

            def refresh(old_token: str, reason: str) -> str | None:
                account_name = account_by_token.get(old_token) or next((name for name, s in token_by_account.items() if s.get("token") == old_token), "")
                if account_name:
                    fresh = account_session_api.issue_for_account_name(account_name, user=user, force_refresh=True)
                    new_token = (fresh.get("token") or "").strip()
                    if new_token:
                        token_by_account[account_name] = fresh
                        account_by_token[new_token] = account_name
                        account_by_token.pop(old_token, None)
                    return new_token or None
                fresh = account_session_api.issue_for_user(user, force_refresh=True)
                return (fresh.get("token") or "").strip() or None

            def rotate_proxy(old_token: str, reason: str) -> str | None:
                token_key = _media_token_gate_key(old_token)
                current_proxy = (proxy_by_token.get(old_token) or "").strip()
                try:
                    from core.static_proxy_pool import get_fresh_proxy, mark_proxy_dead
                    if current_proxy:
                        mark_proxy_dead(current_proxy, ttl=120)
                    exclude = {p for p in proxy_by_token.values() if p}
                    if current_proxy:
                        exclude.add(current_proxy)
                    proxy = get_fresh_proxy(exclude=exclude) or get_fresh_proxy(exclude={current_proxy} if current_proxy else set())
                except Exception as exc:
                    logger.warning("[MediaAPI] proxy_rotate_on_500 failed token=%s error=%s", token_key, exc)
                    return None
                if proxy:
                    proxy_by_token[old_token] = proxy
                    logger.warning(
                        "[MediaAPI] proxy_rotate_on_500 rotated token=%s old_proxy=%s new_proxy=%s source=proxy.txt reason=%s",
                        token_key,
                        current_proxy or "direct",
                        proxy,
                        reason,
                    )
                    return proxy
                logger.warning("[MediaAPI] proxy_rotate_on_500 cooldown_or_empty token=%s old_proxy=%s", token_key, current_proxy or "direct")
                return None

            def on_result(result):
                prompt = str(getattr(result, "prompt", "") or "")
                bucket = by_prompt.get(prompt) or []
                task = bucket.pop(0) if bucket else None
                if not task:
                    return
                status = getattr(result, "status", "") or ""
                task.status = TaskStatus.COMPLETED if status == "completed" else TaskStatus.FAILED
                task.media_id = getattr(result, "download_url", None)
                task.output_filename = getattr(result, "output_path", None)
                task.error = getattr(result, "error", None)
                task.raw_result = {"engine": "reference-implementation", "account_name": primary.get("account_name"), "project_id": dynamic_project_id, "result": getattr(result, "__dict__", {})}
                store.update_video_task(task)

            dispatch_task_count = len(tasks)
            credit_filter = _reference_filter_passed_tokens(
                tokens,
                proxies,
                token_project_map,
                log_prefix="[MediaAPI][ReferenceImplementation]",
            )
            tokens = credit_filter["tokens"]
            proxies = credit_filter["proxies"]
            token_project_map = credit_filter["token_project_map"]
            proxy_by_token = {
                token_value: (proxies[index] if index < len(proxies) else "")
                for index, token_value in enumerate(tokens)
            }
            if not tokens:
                raise RuntimeError("No usable token after reference credit check")
            token_capacity = max(1, len(tokens) * media_per_token_threads)
            # User-requested invariant:
            #   N valid tokens => N long-lived token Chrome runtimes.
            #   Each token/Chrome exposes at most media_per_token_threads lanes.
            # Do not shrink the token set for small batches; otherwise repeated
            # small batches only ever warm token #1 and only one Chrome appears.
            effective_tokens = list(tokens)
            effective_proxies = [proxies[i] if i < len(proxies) else "" for i in range(len(effective_tokens))]
            needed_tokens = len(effective_tokens)
            effective_token_project_map = {
                token_value: token_project_map.get(token_value, dynamic_project_id)
                for token_value in effective_tokens
            }
            effective_primary_token = effective_tokens[0] if effective_tokens else token
            effective_project_id = effective_token_project_map.get(effective_primary_token) or dynamic_project_id
            effective_thread_count = max(1, min(dispatch_task_count, len(effective_tokens) * media_per_token_threads))
            logger.info(
                "[MediaAPI] dispatch jobs=%s tokens_used=%s original_tokens=%s token_capacity=%s thread_count=%s per_token_threads=%s chrome_per_token=1 proxy_source=%s mode=%s",
                dispatch_task_count,
                len(effective_tokens),
                len(tokens),
                token_capacity,
                effective_thread_count,
                media_per_token_threads,
                "runtime_proxies" if runtime_proxies else "proxy.txt",
                mode,
            )

            with _media_acquire_token_chrome_locks(effective_tokens):
                results = run_reference_jobs(
                    token=effective_primary_token,
                    project_id=effective_project_id,
                    prompts=[(task.prompts or [""])[0] for task in tasks],
                    mode=mode,
                    model=("GEM_PIX_2" if tasks[0].model in ("NARWHAL", "GEMINI", "GEM_PIX_2") else tasks[0].model),
                    aspect_ratio=tasks[0].screen_ratio,
                    thread_count=effective_thread_count,
                    max_attempts=5,
                    output_dir=os.path.join(ROOT_DIR, "output", "media_api"),
                    reference_paths=image_paths or [],
                    end_reference_paths=end_image_paths or [],
                    video_type=video_type,
                    image_resolution=image_resolution,
                    output_prefix=tasks[0].project_id,
                    refresh_token_callback=refresh,
                    rotate_proxy_callback=rotate_proxy,
                    result_callback=on_result,
                    tokens=effective_tokens,
                    token_project_map=effective_token_project_map,
                    proxies=effective_proxies,
                )
            seen_ids = {t.id for bucket in by_prompt.values() for t in bucket}
            for task in tasks:
                if task.id not in seen_ids:
                    continue
                idx = tasks.index(task)
                result = results[idx] if idx < len(results) else {}
                ok = result.get("status") == "completed" if isinstance(result, dict) else False
                task.status = TaskStatus.COMPLETED if ok else TaskStatus.FAILED
                task.media_id = result.get("download_url") if isinstance(result, dict) else None
                task.output_filename = result.get("saved_path") if isinstance(result, dict) else None
                task.error = result.get("error") if isinstance(result, dict) else "Task failed"
                task.raw_result = {"engine": "reference-implementation", "account_name": primary.get("account_name"), "project_id": dynamic_project_id, "result": result}
                store.update_video_task(task)
        except Exception as exc:
            logger.exception("[MediaAPI] Batch failed")
            for task in tasks:
                task.status = TaskStatus.FAILED
                task.error = str(exc)
                task.raw_result = {"engine": "reference-implementation", "error": str(exc)}
                store.update_video_task(task)
    threading.Thread(target=runner, daemon=True).start()


def _media_create_task(action_type: str, payload: dict, *, mode: str, image_paths=None, end_image_paths=None, video_type="single", image_resolution="1K"):
    prompts = _media_prompt_lines(payload)
    if not prompts:
        raise ValidationError("prompt is required")
    count = max(1, min(safe_int(payload.get("count", 1), 1), 100))
    total = len(prompts) * count
    project = _media_public_project(
        "Media API",
        total_videos=total,
        project_id=payload.get("project_id"),
        project_name=payload.get("new_project_name") or payload.get("project_name"),
    )
    user = _media_reference_user()
    tasks = []
    for prompt in prompts:
        for idx in range(count):
            suffix = f" #{idx + 1}" if count > 1 else ""
            task = VideoTask(
                id=str(uuid.uuid4()),
                project_id=project.id,
                name=f"{payload.get('model') or 'MEDIA'}: {prompt[:40]}{suffix}",
                action_type=action_type,
                model=payload.get("model") or ("NARWHAL" if action_type == ActionType.CREATE_IMAGE else "FAST"),
                screen_ratio=payload.get("screen_ratio") or "16:9",
                prompts=[prompt],
                status=TaskStatus.PENDING,
                image_refs=image_paths or None,
                count=1,
                raw_result={
                    "image_paths": image_paths or [],
                    "reference_paths": image_paths or [],
                    "end_image_paths": end_image_paths or [],
                    "video_type": video_type,
                    "image_resolution": image_resolution,
                },
            )
            store.create_video_task(task)
            tasks.append(task)
    auto_dispatch = _public_dispatch_tasks(tasks) if tasks else {"dispatched": [], "skipped": []}
    dispatched_ids = {item.get("job_id") for item in auto_dispatch.get("dispatched", [])}
    local_tasks = [task for task in tasks if task.id not in dispatched_ids]
    if auto_dispatch.get("dispatched"):
        logger.info(
            "[MediaAPI][PublicDispatch] dispatched=%s fallback_local=%s skipped=%s",
            len(auto_dispatch.get("dispatched", [])),
            len(local_tasks),
            len(auto_dispatch.get("skipped", [])),
        )
        _PUBLIC_DISPATCH_STATS.update({
            "last_dispatch_at": _now_ts(),
            "dispatch_count": _PUBLIC_DISPATCH_STATS.get("dispatch_count", 0) + len(auto_dispatch.get("dispatched", [])),
        })
    if local_tasks:
        required_error = "Public Machine dispatch required; local fallback disabled"
        logger.error(
            "[MediaAPI][PublicDispatch] required_failed fallback_local=%s skipped=%s reasons=%s",
            len(local_tasks),
            len(auto_dispatch.get("skipped", [])),
            auto_dispatch.get("skipped", []),
        )
        for task in local_tasks:
            task.status = TaskStatus.FAILED
            task.error = required_error
            raw = task.raw_result if isinstance(task.raw_result, dict) else {}
            raw["public_dispatch_required"] = True
            raw["public_dispatch_error"] = auto_dispatch.get("skipped", [])
            task.raw_result = raw
            store.update_video_task(task)
    if project.total_videos != total:
        project.total_videos = max(project.total_videos or 0, total)
        store.update_project(project)
    first = tasks[0].to_public_dict() if tasks else {}
    data = {"items": [task.to_public_dict() for task in tasks], "total": len(tasks), "project_id": project.id, "project": project.to_dict(), **first}
    return success(data, 202)


@app.route("/api/veo/images", methods=["POST"])
@app.route("/api/veo/images/4k", methods=["POST"])
def media_create_images_alias():
    payload = _media_collect_payload()
    is_4k = request.path.rstrip("/").endswith("/4k")
    image_paths = _media_normalize_paths(payload)
    return _media_create_task(ActionType.CREATE_IMAGE, payload, mode="image", image_paths=image_paths, image_resolution="4K" if is_4k else "1K")


@app.route("/api/veo/images/from-images", methods=["POST"])
@app.route("/api/veo/images/from-images/4k", methods=["POST"])
def media_create_images_from_images_alias():
    payload = _media_collect_payload()
    image_paths = _media_normalize_paths(payload)
    if not image_paths:
        raise ValidationError("image_paths/images/files is required")
    is_4k = request.path.rstrip("/").endswith("/4k")
    return _media_create_task(ActionType.CREATE_IMAGE, payload, mode="image", image_paths=image_paths, image_resolution="4K" if is_4k else "1K")


@app.route("/api/veo/videos/from-start-image", methods=["POST"])
def media_video_from_start_alias():
    payload = _media_collect_payload()
    prompts = _media_prompt_lines(payload)
    image_paths = _media_normalize_paths(payload)
    if len(image_paths) < max(1, len(prompts)):
        raise ValidationError(f"need at least {max(1, len(prompts))} start/reference images for {max(1, len(prompts))} prompts")
    return _media_create_task(ActionType.IMAGE_TO_VIDEO, payload, mode="video", image_paths=image_paths, video_type="single")


@app.route("/api/veo/videos/from-reference-image", methods=["POST"])
def media_video_from_reference_alias():
    payload = _media_collect_payload()
    prompts = _media_prompt_lines(payload)
    image_paths = _media_normalize_paths(payload)
    if len(image_paths) < max(1, len(prompts)):
        raise ValidationError(f"need at least {max(1, len(prompts))} reference images for {max(1, len(prompts))} prompts")
    return _media_create_task(ActionType.IMAGE_TO_VIDEO, payload, mode="video", image_paths=image_paths, video_type="single")


@app.route("/api/veo/videos/from-start-end-images", methods=["POST"])
def media_video_from_start_end_alias():
    payload = _media_collect_payload()
    prompts = _media_prompt_lines(payload)
    required = max(1, len(prompts))
    image_paths = _media_normalize_paths(payload)
    end_image_paths = _media_normalize_paths({"image_paths": payload.get("end_image_paths") or payload.get("end_images") or []})
    if len(image_paths) < required:
        raise ValidationError(f"need at least {required} start images for {required} prompts")
    if len(end_image_paths) < required:
        raise ValidationError(f"need at least {required} end images for {required} prompts")
    return _media_create_task(ActionType.FRAMES_TO_VIDEO, payload, mode="video", image_paths=image_paths, end_image_paths=end_image_paths, video_type="start_end")


@app.route("/api/veo/tasks", methods=["GET"])
def media_task_list_alias():
    limit = max(1, min(safe_int(request.args.get("limit", 200), 200), 500))
    project_id = (request.args.get("project_id") or "").strip() or None
    status = (request.args.get("status") or "").strip() or None
    tasks, total = store.list_video_tasks(project_id=project_id, status=status, skip=0, limit=limit)
    items = [task.to_public_dict() for task in tasks]

    project_names = {p.id: p.name for p in store.list_projects() if p}
    grouped = {}
    for item in items:
        pid = item.get("project_id") or "__none__"
        if pid not in grouped:
            grouped[pid] = {
                "project_id": item.get("project_id") or "",
                "project_name": project_names.get(item.get("project_id") or "", "Không có project"),
                "total": 0,
                "completed": 0,
                "failed": 0,
                "processing": 0,
                "pending": 0,
                "started_at": None,
                "finished_at": None,
                "duration_seconds": None,
                "tasks": [],
            }
        group = grouped[pid]
        group["tasks"].append(item)
        group["total"] += 1
        task_status = str(item.get("status") or "").upper()
        if task_status == "COMPLETED":
            group["completed"] += 1
        elif task_status in ("FAILED", "ERROR"):
            group["failed"] += 1
        elif task_status in ("PROCESSING", "CHECKING"):
            group["processing"] += 1
        else:
            group["pending"] += 1

        created_at = item.get("created_at") or None
        finished_at = item.get("completed_at") or item.get("updated_at") or None
        if created_at and (group["started_at"] is None or created_at < group["started_at"]):
            group["started_at"] = created_at
        if finished_at and (group["finished_at"] is None or finished_at > group["finished_at"]):
            group["finished_at"] = finished_at

    project_cards = list(grouped.values())
    for group in project_cards:
        if group["started_at"] and group["finished_at"]:
            group["duration_seconds"] = max(0, round(group["finished_at"] - group["started_at"], 2))
        group["tasks"].sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    project_cards.sort(key=lambda x: x.get("started_at") or 0, reverse=True)

    return success({"items": items, "projects": project_cards, "total": total, "limit": limit, "project_id": project_id, "status": status})


@app.route("/api/veo/tasks/<task_id>", methods=["GET"])
def media_task_status_alias(task_id):
    task = store.get_video_task(task_id)
    if not task:
        raise VietAutoAPIError("Task not found", 404)
    return success(task.to_public_dict())


@app.route("/api/veo/create-image", methods=["POST"])
@require_api_key
@require_permission(Permission.CREATE_VIDEO)
def create_image_route():
    import uuid
    if request.content_type and "multipart/form-data" in request.content_type:
        body = request.form.to_dict()
    else:
        body = request.get_json(silent=True) or {}

    validate_required(body, "project_id", "prompt")

    project_id = body["project_id"]
    prompt = body["prompt"].strip()
    model = body.get("model", "NARWHAL")
    aspect = body.get("aspect", "IMAGE_ASPECT_RATIO_PORTRAIT")
    count = safe_int(body.get("count", 1), 1)
    max_threads = safe_int(body.get("max_threads", body.get("thread_count", 1)), 1)
    upsample_resolution = body.get("upsample_resolution", None)  # None | "1K" | "2K" | "4K"
    banana_access_token = (body.get("access_token") or body.get("google_access_token") or "").strip()
    if banana_access_token.startswith("Bearer "):
        banana_access_token = banana_access_token[7:].strip()
    
    image_refs_raw = body.get("image_refs", [])
    image_refs_b64_raw = body.get("image_refs_b64", [])  # [{b64, mime, name}] from browser file picker
    veo_cookie = banana_access_token
    proxy_url = body.get("proxy_url", None)

    upload_dir = os.path.join(ROOT_DIR, "output", "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    
    image_refs_parsed = []

    if isinstance(image_refs_raw, list):
        for path_str in image_refs_raw:
            if isinstance(path_str, str) and path_str.strip():
                image_refs_parsed.append(path_str.strip())

    import base64
    import mimetypes
    for img in image_refs_b64_raw:
        if isinstance(img, dict) and "b64" in img:
            try:
                img_data = base64.b64decode(img["b64"])
                ext = mimetypes.guess_extension(img.get("mime", "image/jpeg")) or ".jpg"
                filepath = os.path.abspath(os.path.join(upload_dir, f"upload_{uuid.uuid4().hex[:12]}{ext}"))
                with open(filepath, "wb") as f:
                    f.write(img_data)
                image_refs_parsed.append(filepath)
            except Exception as e:
                pass

    if request.content_type and "multipart/form-data" in request.content_type:
        files = request.files.getlist("images[]") or request.files.getlist("images") or request.files.getlist("file")
        for f in files:
            if f and f.filename:
                ext = os.path.splitext(f.filename)[1] or ".jpg"
                filepath = os.path.abspath(os.path.join(upload_dir, f"upload_form_{uuid.uuid4().hex[:12]}{ext}"))
                f.save(filepath)
                image_refs_parsed.append(filepath)

    project = store.get_project(project_id)
    if not project:
        raise VietAutoAPIError(f"Project '{project_id}' not found", 404)

    from web.models import ActionType
    image_refs_combined = image_refs_parsed if image_refs_parsed else None
    count = max(1, min(count, 4))  # clamp 1-4

    task_name = f"{model}: {prompt[:40]}"
    task = VideoTask(
        id=str(uuid.uuid4()),
        project_id=project_id,
        name=task_name,
        action_type=ActionType.CREATE_IMAGE,
        model=model,
        screen_ratio=aspect,
        prompts=[prompt],
        status=TaskStatus.PENDING,
        veo_cookie=veo_cookie,
        proxy_url=proxy_url,
        image_refs=image_refs_combined,
        count=count,
    )
    store.add_video_task(task)

    def _run_reference_image_task():
        try:
            task.status = TaskStatus.PROCESSING
            store.update_video_task(task)

            from web.account_session_api import account_session_api
            from core.reference_runtime import run_reference_jobs

            session = account_session_api.issue_for_user(g.user)
            token = (session.get("token") or "").strip()
            dynamic_project_id = (session.get("project_id") or "").strip()
            if not token or not dynamic_project_id:
                raise RuntimeError("account-session did not return token/project_id")

            def _refresh_reference_token(old_token: str, reason: str) -> str | None:
                try:
                    fresh_session = account_session_api.issue_for_user(g.user, force_refresh=True)
                    fresh_token = (fresh_session.get("token") or "").strip()
                    if fresh_token and fresh_token != old_token:
                        return fresh_token
                except Exception as refresh_error:
                    pass
                return None

            current_proxy_by_token = {token: ""}

            def _rotate_reference_proxy(old_token: str, reason: str) -> str | None:
                token_key = _media_token_gate_key(old_token)
                current_proxy = (current_proxy_by_token.get(old_token) or "").strip()
                try:
                    from core.static_proxy_pool import get_fresh_proxy, mark_proxy_dead
                    if current_proxy:
                        mark_proxy_dead(current_proxy, ttl=120)
                    proxy = get_fresh_proxy(exclude={current_proxy} if current_proxy else set())
                except Exception as exc:
                    logger.warning("[MediaAPI] proxy_rotate_on_500 failed token=%s error=%s", token_key, exc)
                    return None
                if proxy:
                    current_proxy_by_token[old_token] = proxy
                    logger.warning(
                        "[MediaAPI] proxy_rotate_on_500 rotated token=%s old_proxy=%s new_proxy=%s source=proxy.txt reason=%s",
                        token_key,
                        current_proxy or "direct",
                        proxy,
                        reason,
                    )
                    return proxy
                logger.warning("[MediaAPI] proxy_rotate_on_500 cooldown_or_empty token=%s old_proxy=%s", token_key, current_proxy or "direct")
                return None

            aspect_map = {
                "IMAGE_ASPECT_RATIO_PORTRAIT": "9:16",
                "IMAGE_ASPECT_RATIO_LANDSCAPE": "16:9",
                "IMAGE_ASPECT_RATIO_SQUARE": "1:1",
            }
            ref_model = "GEM_PIX_2" if model in ("NARWHAL", "GEMINI", "GEM_PIX_2") else model
            output_dir = os.path.join(ROOT_DIR, "output", "reference_images")

            prompts = [prompt for _ in range(count)]
            results = run_reference_jobs(
                token=token,
                project_id=dynamic_project_id,
                prompts=prompts,
                mode="image",
                model=ref_model,
                aspect_ratio=aspect_map.get(aspect, aspect),
                thread_count=max(1, min(int(max_threads or 1), 2)),
                max_attempts=5,
                output_dir=output_dir,
                reference_paths=image_refs_combined,
                image_resolution=upsample_resolution or "1K",
                output_prefix=task.id,
                refresh_token_callback=_refresh_reference_token,
                rotate_proxy_callback=_rotate_reference_proxy,
            )

            task.status = TaskStatus.COMPLETED if any(r.get("status") == "completed" for r in results) else TaskStatus.FAILED
            task.raw_result = {
                "engine": "reference-implementation",
                "account_name": session.get("account_name"),
                "project_id": dynamic_project_id,
                "results": results,
            }
            failed_errors = [r.get("error") for r in results if r.get("error")]
            if failed_errors and task.status == TaskStatus.FAILED:
                task.error_message = failed_errors[0]
            store.update_video_task(task)
        except Exception as exc:
            logger.exception(f"[CreateImageReference] Task {task.id} failed")
            task.status = TaskStatus.FAILED
            task.error_message = str(exc)
            task.raw_result = {"engine": "reference-implementation", "error": str(exc)}
            store.update_video_task(task)

    import threading
    threading.Thread(target=_run_reference_image_task, daemon=True).start()

    return success(task.to_public_dict(), 202)


@app.route("/api/veo/create-i2v", methods=["POST"])
@require_api_key
@require_permission(Permission.CREATE_VIDEO)
def create_i2v_route():
    body = request.get_json(silent=True) or {}
    validate_required(body, "project_id", "images_b64")

    project_id = body["project_id"]
    prompt = body.get("prompt", "").strip()
    model = body.get("model", "FAST")
    screen_ratio = body.get("screen_ratio", "16:9")
    images_b64_raw = body.get("images_b64", [])   # [{b64, mime, name}]
    mode = body.get("mode", "multi")           # "multi" | "frames"
    veo_cookie = body.get("veo_cookie", None)
    proxy_url = body.get("proxy_url", None)

    if not images_b64_raw:
        raise VietAutoAPIError("Cần ít nhất 1 ảnh.", 400)

    import base64
    import mimetypes
    import re as _re
    import requests as _dl_requests
    upload_dir = os.path.join(ROOT_DIR, "output", "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    images_b64 = []
    for img in images_b64_raw:
        if isinstance(img, dict) and "b64" in img:
            try:
                img_data = base64.b64decode(img["b64"])
                ext = mimetypes.guess_extension(img.get("mime", "image/jpeg")) or ".jpg"
                filepath = os.path.abspath(os.path.join(upload_dir, f"upload_{uuid.uuid4().hex[:12]}{ext}"))
                with open(filepath, "wb") as f:
                    f.write(img_data)
                images_b64.append({"path": filepath, "mime": img.get("mime", "image/jpeg"), "name": img.get("name", "upload.jpg")})
            except Exception as e:
                pass
        elif isinstance(img, dict) and "url" in img:
            try:
                raw_url = img["url"]
                gdrive_match = _re.search(r"/file/d/([a-zA-Z0-9_-]+)", raw_url)
                if gdrive_match:
                    file_id = gdrive_match.group(1)
                    raw_url = f"https://drive.google.com/uc?export=download&id={file_id}"
                dl_resp = _dl_requests.get(raw_url, timeout=60, allow_redirects=True)
                if dl_resp.status_code == 200 and len(dl_resp.content) > 0:
                    ct = dl_resp.headers.get("Content-Type", "image/jpeg")
                    ext = mimetypes.guess_extension(ct.split(";")[0].strip()) or ".jpg"
                    filepath = os.path.abspath(os.path.join(upload_dir, f"dl_{uuid.uuid4().hex[:12]}{ext}"))
                    with open(filepath, "wb") as f:
                        f.write(dl_resp.content)
                    images_b64.append({"path": filepath, "mime": ct.split(";")[0].strip(), "name": img.get("name", filepath.split(os.sep)[-1])})
                else:
                    pass
            except Exception as e:
                pass
        elif isinstance(img, dict) and "path" in img:
            raw_path = img["path"]
            clean_path = str(raw_path).strip().strip('"').strip("'")
            if not os.path.exists(clean_path):
                raise VietAutoAPIError(f"LỖI: File ảnh '{clean_path}' KHÔNG TỒN TẠI trên máy! Vui lòng sửa lại Excel.", 400)
            img["path"] = clean_path
            images_b64.append(img)
    
    if not images_b64:
        raise VietAutoAPIError("Giai ma anh that bai.", 400)

    project = store.get_project(project_id)
    if not project:
        project = Project(
            id=project_id,
            name=(body.get("project_name") or body.get("name") or "I2V Auto Project").strip(),
        )
        store.create_project(project)

    veo = get_veo_service()
    if not veo:
        raise VietAutoAPIError("Chưa setup Cookie Veo.", 500)

    from web.models import ActionType

    tasks_created = []
    created_task_objs = []  # giữ VideoTask objects để submit pre-upload bucket

    prompt_lines = [line.strip() for line in str(prompt or "").replace("\r\n", "\n").replace("\r", "\n").split("\n") if line.strip()]
    if not prompt_lines:
        prompt_lines = [""]

    if mode == "frames":
        imgs = images_b64[:2]
        act = ActionType.FRAMES_TO_VIDEO if len(imgs) >= 2 else ActionType.IMAGE_TO_VIDEO
        for pidx, one_prompt in enumerate(prompt_lines):
            task = VideoTask(
                id=str(uuid.uuid4()),
                project_id=project_id,
                name=f"I2V-Frames #{pidx+1}: {(one_prompt or imgs[0].get('name','img'))[:35]}",
                action_type=act,
                model=model,
                screen_ratio=screen_ratio,
                prompts=[one_prompt],
                status=TaskStatus.PENDING,
                veo_cookie=veo_cookie,
                proxy_url=proxy_url,
            )
            task.raw_result = {"images_b64": imgs, "mode": "frames"}
            store.create_video_task(task)
            created_task_objs.append(task)
            tasks_created.append(task.to_public_dict())
    else:
        for i, img in enumerate(images_b64[:10]):   # max 10
            fname = img.get("name", f"image_{i+1}.jpg")
            for pidx, one_prompt in enumerate(prompt_lines):
                task = VideoTask(
                    id=str(uuid.uuid4()),
                    project_id=project_id,
                    name=f"I2V #{i+1}.{pidx+1}: {fname[:30]}",
                    action_type=ActionType.IMAGE_TO_VIDEO,
                    model=model,
                    screen_ratio=screen_ratio,
                    prompts=[one_prompt],
                    status=TaskStatus.PENDING,
                    veo_cookie=veo_cookie,
                    proxy_url=proxy_url,
                )
                task.raw_result = {"images_b64": [img], "mode": "single"}
                store.create_video_task(task)
                created_task_objs.append(task)
                tasks_created.append(task.to_public_dict())

    dispatch_candidates = [t for t in created_task_objs if not getattr(t, "veo_cookie", None)]
    auto_dispatch = _public_dispatch_tasks(dispatch_candidates) if dispatch_candidates else {"dispatched": [], "skipped": []}
    dispatched_ids = {item.get("job_id") for item in auto_dispatch.get("dispatched", [])}
    if auto_dispatch.get("dispatched"):
        logger.info(
            "[I2V][PublicDispatch] dispatched=%s fallback_local=%s skipped=%s",
            len(auto_dispatch.get("dispatched", [])),
            len([t for t in created_task_objs if t.id not in dispatched_ids]),
            len(auto_dispatch.get("skipped", [])),
        )
        _PUBLIC_DISPATCH_STATS.update({
            "last_dispatch_at": _now_ts(),
            "dispatch_count": _PUBLIC_DISPATCH_STATS.get("dispatch_count", 0) + len(auto_dispatch.get("dispatched", [])),
        })

    local_task_objs = [t for t in created_task_objs if t.id not in dispatched_ids]
    own_cookie_tasks = [t for t in local_task_objs if getattr(t, "veo_cookie", None)]
    pool_tasks = [t for t in local_task_objs if not getattr(t, "veo_cookie", None)]
    if own_cookie_tasks or pool_tasks:
        required_error = "Public Machine dispatch required; local fallback disabled"
        logger.error(
            "[I2V][PublicDispatch] required_failed fallback_local=%s own_cookie=%s pool=%s skipped=%s reasons=%s",
            len(local_task_objs),
            len(own_cookie_tasks),
            len(pool_tasks),
            len(auto_dispatch.get("skipped", [])),
            auto_dispatch.get("skipped", []),
        )
        for t in local_task_objs:
            t.status = TaskStatus.FAILED
            t.error = required_error
            raw = t.raw_result if isinstance(t.raw_result, dict) else {}
            raw["public_dispatch_required"] = True
            raw["public_dispatch_error"] = auto_dispatch.get("skipped", [])
            t.raw_result = raw
            store.update_video_task(t)

    response_data = tasks_created[0] if len(tasks_created) == 1 else tasks_created
    return success(response_data, 202)


@app.route("/api/veo/project/<project_id>", methods=["GET"])
@require_api_key
def get_project(project_id):
    p = store.get_project(project_id)
    if not p:
        raise VietAutoAPIError(f"Project '{project_id}' not found", 404)
    return success(p.to_dict())


@app.route("/api/veo/project/<project_id>", methods=["DELETE"])
@require_api_key
def delete_project(project_id):
    project = store.get_project(project_id)
    if not project:
        raise VietAutoAPIError(f"Project '{project_id}' not found", 404)

    media_paths = []
    try:
        docs = store._video_tasks.find({"project_id": project_id}, {"media_id": 1})
        for doc in docs:
            mid = doc.get("media_id")
            if not isinstance(mid, str):
                mid = mid[0] if isinstance(mid, list) and mid else None
            if mid and not mid.startswith("http"):
                media_paths.append(mid)
    except Exception as e:
        pass

    deleted = store.delete_project(project_id)
    if not deleted:
        raise VietAutoAPIError(f"Project '{project_id}' not found", 404)

    def _cleanup(paths):
        removed = 0
        for mid in paths:
            if os.path.isfile(mid):
                try:
                    os.remove(mid)
                    removed += 1
                except Exception as e:
                    pass

    if media_paths:
        threading.Thread(target=_cleanup, args=(media_paths,), daemon=True).start()
    return success({"deleted": project_id, "files_cleanup_queued": len(media_paths)})


@app.route("/api/veo/project/<project_id>/download", methods=["GET"])
def download_project_zip(project_id):
    import io, zipfile as _zip

    project = store.get_project(project_id)
    if not project:
        raise VietAutoAPIError(f"Project '{project_id}' not found", 404)

    tasks, _ = store.list_video_tasks(project_id=project_id, limit=9999)

    zip_buffer = io.BytesIO()
    files_added = 0
    with _zip.ZipFile(zip_buffer, "w", _zip.ZIP_DEFLATED) as zf:
        for task in tasks:
            if task.status != TaskStatus.COMPLETED or not task.media_id:
                continue

            is_image_task = getattr(task, "action_type", "") in ("CREATE_IMAGE", "create_image")
            if is_image_task and isinstance(getattr(task, "raw_result", None), dict) and task.raw_result.get("image_paths"):
                for img_path in task.raw_result["image_paths"]:
                    if not img_path.startswith("http") and not os.path.isfile(img_path):
                        continue
                    
                    sub_arcname = os.path.basename(img_path)
                    existing = [zi.filename for zi in zf.infolist()]
                    if sub_arcname in existing:
                        sub_arcname = f"{task.id[:8]}_{sub_arcname}"
                        
                    if img_path.startswith("http"):
                        try:
                            import urllib.request
                            from urllib.request import Request
                            req = Request(img_path, headers={'User-Agent': 'Mozilla/5.0'})
                            with urllib.request.urlopen(req, timeout=60) as resp:
                                zf.writestr(sub_arcname, resp.read())
                            files_added += 1
                        except Exception as e:
                            pass
                    else:
                        zf.write(img_path, sub_arcname)
                        files_added += 1
                continue

            arcname = task.output_filename or task.name or task.id[:8]
            ext = os.path.splitext(arcname)[1].lower()
            if not ext:
                if is_image_task:
                    arcname += ".png"
                else:
                    arcname += ".mp4"
            else:
                if is_image_task and ext == ".mp4":
                    arcname = arcname.rsplit(".mp4", 1)[0]
                    if not arcname.endswith(".png") and not arcname.endswith(".jpg") and not arcname.endswith(".webp"):
                         arcname += ".png"

            existing = [zi.filename for zi in zf.infolist()]
            if arcname in existing:
                arcname = f"{task.id[:8]}_{arcname}"

            if task.media_id.startswith("http"):
                try:
                    import urllib.request
                    with urllib.request.urlopen(task.media_id, timeout=60) as resp:
                        zf.writestr(arcname, resp.read())
                    files_added += 1
                except Exception as e:
                    pass
            else:
                if os.path.isfile(task.media_id):
                    zf.write(task.media_id, arcname)
                    files_added += 1

    if files_added == 0:
        raise VietAutoAPIError("Không có video nào có thể tải xuống.", 404)

    zip_buffer.seek(0)
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in project.name)
    from flask import send_file as _send_file
    return _send_file(
        zip_buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{safe_name}_videos.zip",
    )


@app.route("/api/veo/image/<task_id>", methods=["GET"])
@require_api_key
def serve_image(task_id):
    task = store.get_video_task(task_id)
    if not task:
        raise VietAutoAPIError(f"Task '{task_id}' not found", 404)
    if task.status != TaskStatus.COMPLETED:
        raise VietAutoAPIError("Task chưa hoàn thành", 400)

    idx = request.args.get("idx", 0, type=int)

    paths = []
    if isinstance(task.raw_result, dict):
        paths = task.raw_result.get("image_paths", [])

    media = None
    if paths and 0 <= idx < len(paths):
        media = paths[idx]
    elif task.media_id:
        media = task.media_id

    if not media:
        raise VietAutoAPIError("Không tìm thấy ảnh", 404)

    if os.path.isfile(media):
        ext = os.path.splitext(media)[1].lower()
        mime_map = {".png": "image/png", ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg", ".webp": "image/webp"}
        mime = mime_map.get(ext, "image/png")
        return send_file(media, mimetype=mime)

    if media.startswith("http"):
        from flask import redirect
        return redirect(media)

    raise VietAutoAPIError("Không thể serve ảnh", 404)


@app.route("/api/veo/ref-image/<task_id>", methods=["GET"])
@require_api_key
def serve_ref_image(task_id):
    task = store.get_video_task(task_id)
    if not task:
        raise VietAutoAPIError(f"Task '{task_id}' not found", 404)

    idx = request.args.get("idx", 0, type=int)

    if task.image_refs and isinstance(task.image_refs, list):
        if 0 <= idx < len(task.image_refs):
            ref = task.image_refs[idx]
            if isinstance(ref, dict):
                fpath = ref.get("path", "")
            else:
                fpath = str(ref)
            if os.path.isfile(fpath):
                ext = os.path.splitext(fpath)[1].lower()
                mime_map = {".png": "image/png", ".jpg": "image/jpeg",
                            ".jpeg": "image/jpeg", ".webp": "image/webp"}
                mime = mime_map.get(ext, "image/jpeg")
                return send_file(fpath, mimetype=mime)

    raw = task.raw_result if isinstance(task.raw_result, dict) else {}
    images_b64 = raw.get("images_b64") or []
    if images_b64 and 0 <= idx < len(images_b64):
        img = images_b64[idx]
        if isinstance(img, dict):
            fpath = img.get("path", "")
            if fpath and os.path.isfile(fpath):
                ext = os.path.splitext(fpath)[1].lower()
                mime_map = {".png": "image/png", ".jpg": "image/jpeg",
                            ".jpeg": "image/jpeg", ".webp": "image/webp"}
                mime = mime_map.get(ext, img.get("mime", "image/jpeg"))
                return send_file(fpath, mimetype=mime)

    image_path = raw.get("image_path")
    if image_path and idx == 0 and os.path.isfile(image_path):
        ext = os.path.splitext(image_path)[1].lower()
        mime_map = {".png": "image/png", ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg", ".webp": "image/webp"}
        mime = mime_map.get(ext, "image/jpeg")
        return send_file(image_path, mimetype=mime)

    raise VietAutoAPIError("Không tìm thấy ảnh tham chiếu", 404)


@app.route("/api/veo/retry/<task_id>", methods=["POST"])
@require_api_key
@require_permission(Permission.CREATE_VIDEO)  # Assuming Retry creates video
def retry_video_task(task_id):
    task = store.get_video_task(task_id)
    if not task:
        raise VietAutoAPIError(f"VideoTask '{task_id}' not found", 404)

    if task.status == TaskStatus.COMPLETED:
        raise VietAutoAPIError(
            "Không thể retry task đã COMPLETED.", 400
        )

    veo = get_veo_service()
    if not veo:
        raise VietAutoAPIError("Chưa setup Cookie Veo.", 500)

    body = request.get_json(silent=True) or {}
    if "prompt" in body and body["prompt"].strip():
        task.prompts = [body["prompt"].strip()]
    if "screen_ratio" in body and body["screen_ratio"].strip():
        task.screen_ratio = body["screen_ratio"].strip()
    if "proxy_url" in body and body["proxy_url"]:
        task.proxy_url = body["proxy_url"]
    if "veo_cookie" in body and body["veo_cookie"]:
        task.veo_cookie = body["veo_cookie"]

    if "model" in body and body["model"].strip():
        new_model = body["model"].strip()
        from web.models import ImageModel
        image_model_values = {m.value for m in ImageModel}

        if new_model in image_model_values:
            task.model = new_model
            task.action_type = ActionType.CREATE_IMAGE
        elif task.action_type == ActionType.CREATE_IMAGE:
            task.model = "NARWHAL"
            task.action_type = ActionType.CREATE_IMAGE
        else:
            task.model = new_model

    import time as _time
    task.status = TaskStatus.PENDING
    task.error = None
    task.veo_cookie = None
    task.picked_account_name = None
    task.created_at = _time.time()
    store.update_video_task(task)

    try:
        if task.action_type == ActionType.TEXT_TO_VIDEO:
            veo.enqueue(task, veo._run_t2v)
        elif task.action_type in (ActionType.IMAGE_TO_VIDEO, ActionType.FRAMES_TO_VIDEO):
            raw = task.raw_result or {}
            if not raw.get("images_b64") and not raw.get("image_path"):
                task.status = TaskStatus.FAILED
                task.error = (
                    "⚠ Task này không thể retry vì ảnh gốc không còn trong hệ thống.\n"
                    "Vui lòng tạo lại task I2V mới và upload lại ảnh."
                )
                store.update_video_task(task)
                return success(task.to_dict())
            img_path = raw.get("image_path")
            if img_path and not raw.get("images_b64") and not os.path.exists(img_path):
                task.status = TaskStatus.FAILED
                task.error = (
                    f"⚠ Ảnh gốc đã bị xóa: {img_path}\n"
                    "File nằm trong thư mục Temp đã bị hệ thống dọn dẹp.\n"
                    "Vui lòng tạo lại task I2V mới và upload lại ảnh."
                )
                store.update_video_task(task)
                return success(task.to_dict())
            veo.enqueue(task, veo._run_i2v)
        elif task.action_type == ActionType.CREATE_IMAGE:
            veo.enqueue(task, veo._run_create_image)
        else:
            task.status = TaskStatus.FAILED
            task.error = f"Không hỗ trợ retry action_type={task.action_type}"
            store.update_video_task(task)
    except Exception as e:
        task.status = TaskStatus.FAILED
        task.error = str(e)
        store.update_video_task(task)

    return success(task.to_dict())


@app.route("/api/veo/retry-batch", methods=["POST"])
@require_api_key
@require_permission(Permission.CREATE_VIDEO)
def retry_batch_tasks():
    body = request.get_json(silent=True) or {}
    task_ids = body.get("task_ids", [])
    if not task_ids or not isinstance(task_ids, list):
        raise VietAutoAPIError("Thiếu danh sách task_ids", 400)

    veo = get_veo_service()
    if not veo:
        raise VietAutoAPIError("Chưa setup Cookie Veo.", 500)

    import time as _time
    retried = 0
    skipped = 0

    tasks_to_retry = []
    for tid in task_ids:
        task = store.get_video_task(tid)
        if not task:
            skipped += 1
            continue
        if task.status == TaskStatus.COMPLETED:
            skipped += 1
            continue

        task.status = TaskStatus.PENDING
        task.error = None
        task.veo_cookie = None
        task.picked_account_name = None
        task.created_at = _time.time()
        store.update_video_task(task)
        tasks_to_retry.append(task)

    _I2V_TYPES_BATCH = {ActionType.IMAGE_TO_VIDEO, ActionType.IMAGES_TO_VIDEO, ActionType.FRAMES_TO_VIDEO}
    i2v_batch = [t for t in tasks_to_retry if getattr(t, "action_type", None) in _I2V_TYPES_BATCH]
    if i2v_batch and len(i2v_batch) > 1:
        try:
            _proj_id = getattr(i2v_batch[0], "project_id", "") or ""
            veo._pre_upload_images(i2v_batch, _proj_id)
        except Exception as _pre_err:
            pass

    for task in tasks_to_retry:
        try:
            if task.action_type == ActionType.TEXT_TO_VIDEO:
                veo.enqueue(task, veo._run_t2v)
            elif task.action_type in (ActionType.IMAGE_TO_VIDEO, ActionType.FRAMES_TO_VIDEO):
                veo.enqueue(task, veo._run_i2v)
            elif task.action_type == ActionType.CREATE_IMAGE:
                veo.enqueue(task, veo._run_create_image)
            retried += 1
        except Exception as e:
            skipped += 1

    return success({"retried": retried, "skipped": skipped, "total": len(task_ids)})


@app.route("/api/veo/videos", methods=["GET"])
@require_api_key
@require_permission(Permission.VIEW_VIDEO)
def get_all_videos():
    page = safe_int(request.args.get("page", 1), 1)
    limit = safe_int(request.args.get("limit", 20), 20)
    search = request.args.get("search", None)
    status = request.args.get("status", None)
    project_id = request.args.get("project_id", None)
    media_type = request.args.get("media_type", None)   # "IMAGE" | "VIDEO" | "ALL" | None
    date_from_str = request.args.get("date_from", None)  # "YYYY-MM-DD" or unix timestamp

    if search and search.strip() == "":
        search = None
    if status and status.strip() == "":
        status = None
    if project_id and project_id.strip() == "":
        project_id = None
    if media_type and media_type.upper() in ("ALL", ""):
        media_type = None

    date_from = None
    if date_from_str and date_from_str.strip():
        try:
            from datetime import datetime
            dt = datetime.strptime(date_from_str.strip(), "%Y-%m-%d")
            date_from = dt.timestamp()
        except ValueError:
            try:
                date_from = float(date_from_str)
            except (ValueError, TypeError):
                date_from = None

    skip = (page - 1) * limit

    tasks, total_count = store.list_video_tasks(
        project_id=project_id, search=search, status=status, skip=skip, limit=limit,
        media_type=media_type, date_from=date_from,
    )
    data = [
        {
            "id": t.id,
            "status": t.status,
            "project_id": t.project_id,
            "created_at": t.created_at,
            "name": t.name,
            "prompts": t.prompts,
            "model": getattr(t, "model", "FAST"),
            "screen_ratio": getattr(t, "screen_ratio", "16:9"),
            "action_type": getattr(t, "action_type", "text_to_video"),
            "media_id": t.media_id,
            "output_filename": getattr(t, "output_filename", None),
            "file_exists": (
                os.path.exists(t.media_id)
                if t.status == "COMPLETED" and t.media_id and not t.media_id.startswith("http")
                else (t.status == "COMPLETED" and bool(t.media_id))
            ),
            "error": t.error,
            "completed_at": getattr(t, "completed_at", None),
            "image_refs": getattr(t, "image_refs", None),
            "raw_result": {
                "image_paths": (t.raw_result or {}).get("image_paths"),
                "image_path": (t.raw_result or {}).get("image_path"),
                "images_b64": [
                    {"name": img.get("name"), "path": img.get("path")}
                    for img in ((t.raw_result or {}).get("images_b64") or [])
                    if isinstance(img, dict)
                ] or None,
            } if isinstance(t.raw_result, dict) else None,
            "download_url": (
                f"{request.host_url.rstrip('/')}/api/veo/download/{t.id}?token={request.args.get('token')}"
                if t.status == "COMPLETED" and request.args.get('token')
                else (
                    f"{request.host_url.rstrip('/')}/api/veo/download/{t.id}"
                    if t.status == "COMPLETED"
                    else None
                )
            ),
        }
        for t in tasks
    ]

    return success(
        {
            "items": data,
            "total": total_count,
            "page": page,
            "limit": limit,
            "has_more": skip + len(data) < total_count,
        }
    )


@app.route("/api/veo/video", methods=["GET"])
@require_api_key
@require_permission(Permission.VIEW_VIDEO)
def get_video():
    task_id = request.args.get("task_id")
    project_id = request.args.get("project_id")

    if task_id:
        task = store.get_video_task(task_id)
        if not task:
            raise VietAutoAPIError(f"Task '{task_id}' not found", 404)
        return success(task.to_public_dict())

    page = safe_int(request.args.get("page", 1), 1)
    limit = safe_int(request.args.get("limit", 20), 20)
    skip = (page - 1) * limit

    base_url = request.host_url.rstrip("/")
    items = []
    for t in tasks:
        d = t.to_public_dict()
        if t.status == TaskStatus.COMPLETED:
            d["download_url"] = f"{base_url}/api/veo/download/{t.id}"
        items.append(d)

    return success(
        {
            "items": items,
            "total": total_count,
            "page": page,
            "limit": limit,
        }
    )


@app.route("/api/veo/video/<task_id>", methods=["DELETE"])
@require_api_key
@require_role(UserRole.ADMIN)
def delete_video(task_id):
    if not task_id:
        raise ValidationError("task_id is required")

    task = store.get_video_task(task_id)
    if not task:
        raise VietAutoAPIError(f"Task '{task_id}' not found", 404)

    if task.media_id and not task.media_id.startswith("http") and os.path.isfile(task.media_id):
        try:
            os.remove(task.media_id)
        except Exception as e:
            pass

    success_del = store.delete_video_task(task_id)
    if not success_del:
        raise VietAutoAPIError("Failed to delete task", 500)

    return success({"id": task_id, "deleted": True})


@app.route("/api/veo/text-to-video", methods=["POST"])
@require_api_key
@require_permission(Permission.CREATE_VIDEO)
def text_to_video():
    if request.is_json:
        body = request.get_json()
        project_id = body.get("project_id")
        name = body.get("name")
        model = body.get("model", VeoModel.T2V_FAST)
        screen_ratio = body.get("screen_ratio", ScreenRatio.LANDSCAPE)
        prompts = body.get("prompts", [])
    else:
        project_id = request.form.get("project_id")
        name = request.form.get("name")
        model = request.form.get("model", VeoModel.T2V_FAST)
        screen_ratio = request.form.get("screen_ratio", ScreenRatio.LANDSCAPE)
        prompts = request.form.getlist("prompts") or request.form.getlist("prompts[]")

    if not project_id:
        raise ValidationError("project_id is required")
    if not name:
        raise ValidationError("name is required")
    if not prompts:
        raise ValidationError("prompts is required and must be non-empty")
    if not store.get_project(project_id):
        raise VietAutoAPIError(f"Project '{project_id}' not found", 404)

    validate_enum(model, VeoModel, "model")
    if screen_ratio not in ("16:9", "9:16"):
        raise ValidationError("screen_ratio must be '16:9' or '9:16'")

    svc = get_veo_service()
    task = svc.create_text_to_video(
        project_id=project_id,
        name=name,
        model=model,
        screen_ratio=screen_ratio,
        prompts=prompts if isinstance(prompts, list) else [prompts],
        background=True,
    )

    return success(task.to_public_dict(), 202)


@app.route("/api/veo/image-to-video", methods=["POST"])
@require_api_key
@require_permission(Permission.CREATE_VIDEO)
def image_to_video():
    project_id = request.form.get("project_id")
    name = request.form.get("name")
    model = request.form.get("model", VeoModel.I2V_FAST)
    screen_ratio = request.form.get("screen_ratio", ScreenRatio.LANDSCAPE)
    prompts = request.form.getlist("prompts") or request.form.getlist("prompts[]")

    if not project_id:
        raise ValidationError("project_id is required")
    if not name:
        raise ValidationError("name is required")
    if not prompts:
        raise ValidationError("prompts is required")
    if not store.get_project(project_id):
        raise VietAutoAPIError(f"Project '{project_id}' not found", 404)

    file = request.files.get("file") or request.files.get("files")
    if not file:
        raise ValidationError("file (image) is required")

    suffix = os.path.splitext(file.filename)[1] or ".jpg"
    upload_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output", "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    image_path = os.path.join(upload_dir, f"i2v_{uuid.uuid4().hex[:12]}{suffix}")
    file.save(image_path)

    validate_enum(model, VeoModel, "model")
    if screen_ratio not in ("16:9", "9:16"):
        raise ValidationError("screen_ratio must be '16:9' or '9:16'")

    svc = get_veo_service()
    task = svc.create_image_to_video(
        project_id=project_id,
        name=name,
        model=model,
        screen_ratio=screen_ratio,
        prompts=prompts if isinstance(prompts, list) else [prompts],
        image_path=image_path,
        background=True,
    )

    return success(task.to_public_dict(), 202)


_IMAGE_MAGIC = [
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
]


def _detect_image_format(blob: bytes):
    if not blob or len(blob) < 12:
        return None, None
    for magic, mime, ext in _IMAGE_MAGIC:
        if blob.startswith(magic):
            return mime, ext
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None, None


def _verify_image_decodable(blob: bytes) -> "tuple[bool, str]":
    try:
        from PIL import Image as _PILImage  # type: ignore
    except Exception:
        return True, ""  # PIL không có → skip verify, dựa magic bytes
    try:
        _PILImage.open(_io.BytesIO(blob)).verify()
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


@app.route("/api/veo/frames-to-video", methods=["POST"])
@require_api_key
@require_permission(Permission.CREATE_VIDEO)
def frames_to_video():
    project_id = request.form.get("project_id")
    name = request.form.get("name")
    model = request.form.get("model", VeoModel.I2V_FAST)
    screen_ratio = request.form.get("screen_ratio", ScreenRatio.LANDSCAPE)
    prompts = request.form.getlist("prompts") or request.form.getlist("prompts[]")

    if not project_id:
        raise ValidationError("project_id is required")
    if not name:
        raise ValidationError("name is required")
    if not store.get_project(project_id):
        raise VietAutoAPIError(f"Project '{project_id}' not found", 404)

    files = request.files.getlist("files")
    if len(files) < 1:
        raise ValidationError("At least 1 file is required (start frame)")
    if len(files) > 2:
        raise ValidationError(
            f"Tối đa 2 ảnh (start + end). Nhận {len(files)} file."
        )

    validated = []  # list[(blob, mime, ext, original_name)]
    for idx, f in enumerate(files):
        try:
            blob = f.read()
        except Exception as e:
            raise ValidationError(
                f"File {idx+1} ({f.filename!r}): không đọc được stream — {e}"
            )
        if not blob:
            raise ValidationError(
                f"File {idx+1} ({f.filename!r}): rỗng (0 bytes). "
                f"Có thể network ngắt giữa lúc upload."
            )
        mime, ext = _detect_image_format(blob)
        if not mime:
            head_hex = blob[:12].hex()
            head_ascii = blob[:40].decode("ascii", errors="replace")
            raise ValidationError(
                f"File {idx+1} ({f.filename!r}, {len(blob)}B): không phải ảnh hợp lệ "
                f"(magic={head_hex!r}, head={head_ascii!r}). "
                f"Chỉ nhận JPEG/PNG/WEBP/GIF."
            )
        ok, err = _verify_image_decodable(blob)
        if not ok:
            raise ValidationError(
                f"File {idx+1} ({f.filename!r}, {len(blob)}B {mime}): "
                f"ảnh corrupt — PIL không decode được. {err}"
            )
        validated.append((blob, mime, ext, f.filename or f"file{idx+1}"))

    upload_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "output", "uploads",
    )
    os.makedirs(upload_dir, exist_ok=True)
    tmp_paths = []
    for blob, mime, ext, orig_name in validated:
        fpath = os.path.join(upload_dir, f"frame_{uuid.uuid4().hex[:12]}{ext}")
        tmp_path = fpath + ".tmp"
        try:
            with open(tmp_path, "wb") as out:
                out.write(blob)
                out.flush()
                try:
                    os.fsync(out.fileno())  # ép OS flush xuống disk
                except Exception:
                    pass
            os.replace(tmp_path, fpath)
        except Exception as e:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            raise VietAutoAPIError(
                f"Không thể lưu file ảnh (disk error): {e}", 500
            )
        tmp_paths.append(fpath)

    validate_enum(model, VeoModel, "model")

    svc = get_veo_service()
    task = svc.create_frames_to_video(
        project_id=project_id,
        name=name,
        model=model,
        screen_ratio=screen_ratio,
        prompts=prompts if isinstance(prompts, list) else [prompts],
        start_image_path=tmp_paths[0],
        end_image_path=tmp_paths[1] if len(tmp_paths) > 1 else None,
        background=True,
    )

    return success(task.to_public_dict(), 202)


@app.route("/api/veo/download/<task_id>", methods=["GET"])
@require_api_key
@require_permission(Permission.VIEW_VIDEO)
def download_video(task_id):
    task = store.get_video_task(task_id)
    if not task:
        raise VietAutoAPIError(f"Task '{task_id}' not found", 404)

    if task.status == TaskStatus.PENDING or task.status == TaskStatus.PROCESSING:
        return (
            jsonify(
                {
                    "success": False,
                    "error": f"Task is still {task.status}. Poll /api/veo/video?task_id={task_id} to check status.",
                    "status": task.status,
                }
            ),
            202,
        )

    if task.status == TaskStatus.FAILED:
        return (
            jsonify(
                {
                    "success": False,
                    "error": f"Task failed: {task.error}",
                    "status": task.status,
                }
            ),
            422,
        )

    media_id = task.media_id
    if not media_id:
        raise VietAutoAPIError("Task completed but no media_id found.", 500)

    if (
        media_id.startswith("/")
        or "\\" in media_id
        or media_id.startswith("C:")
        or media_id.startswith("d:")
    ):
        if os.path.isfile(media_id):
            import mimetypes
            mime, _ = mimetypes.guess_type(media_id)
            if not mime:
                mime = "video/mp4"
            return send_file(
                media_id,
                mimetype=mime,
                as_attachment=False,
                download_name=os.path.basename(media_id),
            )
        else:
            raise VietAutoAPIError(
                f"Video file no longer exists on server: {media_id}", 404
            )

    if media_id.startswith("http"):
        from flask import redirect as flask_redirect
        return flask_redirect(media_id, 302)

    raise VietAutoAPIError(f"Unexpected media_id format: {media_id[:80]}", 500)


@app.route("/api/veo/wait-for-video", methods=["POST"])
@require_api_key
@require_permission(Permission.CREATE_VIDEO)
def wait_for_video():
    body = request.get_json(silent=True) or {}
    project_id = body.get("project_id")
    name = body.get("name", "API Video")
    model = body.get("model", VeoModel.T2V_FAST)
    screen_ratio = body.get("screen_ratio", "16:9")
    prompts = body.get("prompts", [])

    if not project_id:
        raise ValidationError("project_id is required")
    if not prompts:
        raise ValidationError("prompts is required")
    if not store.get_project(project_id):
        raise VietAutoAPIError(f"Project '{project_id}' not found", 404)
    if screen_ratio not in ("16:9", "9:16"):
        raise ValidationError("screen_ratio must be '16:9' or '9:16'")

    svc = get_veo_service()
    task = svc.create_text_to_video(
        project_id=project_id,
        name=name,
        model=model,
        screen_ratio=screen_ratio,
        prompts=prompts if isinstance(prompts, list) else [prompts],
        background=True,
    )

    return success(
        {
            **task.to_public_dict(),
            "poll_url": f"/api/veo/video?task_id={task.id}",
            "download_url": f"/api/veo/download/{task.id}",
            "message": "Task submitted. Poll poll_url every 10s. When status=COMPLETED, fetch download_url.",
        },
        202,
    )


@app.route("/api/veo/generate", methods=["POST"])
@require_api_key
@require_permission(Permission.CREATE_VIDEO)
def generate_and_return():
    body = request.get_json(silent=True) or {}
    prompt = body.get("prompt", "").strip()
    project_id = body.get("project_id", "")
    model = body.get("model", VeoModel.T2V_FAST)
    screen_ratio = body.get("screen_ratio", "9:16")

    if not prompt:
        raise ValidationError("prompt is required")
    if screen_ratio not in ("16:9", "9:16"):
        raise ValidationError("screen_ratio must be '16:9' or '9:16'")

    if not project_id or not store.get_project(project_id):
        project = Project(id=str(uuid.uuid4()), name=f"auto-{int(time.time())}")
        store.create_project(project)
        project_id = project.id

    svc = get_veo_service()
    task = svc.create_text_to_video(
        project_id=project_id,
        name=prompt[:50],
        model=model,
        screen_ratio=screen_ratio,
        prompts=[prompt],
        background=True,
    )

    base_url = request.host_url.rstrip("/")
    return success(
        {
            "task_id": task.id,
            "status": task.status,
            "poll_url": f"{base_url}/api/veo/video?task_id={task.id}",
            "download_url": f"{base_url}/api/veo/download/{task.id}",
            "message": "Task queued. Poll poll_url every 10s until status=COMPLETED, then GET download_url for the MP4 file.",
        },
        202,
    )


@app.route("/api/veo/create", methods=["POST"])
@require_api_key
@require_permission(Permission.CREATE_VIDEO)
def create_video_stream():
    import json as _json
    from flask import Response

    body = request.get_json(silent=True) or {}
    prompt = body.get("prompt", "").strip()
    screen_ratio = body.get("screen_ratio", "9:16")
    model = body.get("model", VeoModel.T2V_FAST)

    if not prompt:
        raise ValidationError("prompt is required")
    if screen_ratio not in ("16:9", "9:16"):
        raise ValidationError("screen_ratio must be '16:9' or '9:16'")

    project = Project(id=str(uuid.uuid4()), name=f"sse-{int(time.time())}")
    store.create_project(project)

    svc = get_veo_service()
    task = svc.create_text_to_video(
        project_id=project.id,
        name=prompt[:50],
        model=model,
        screen_ratio=screen_ratio,
        prompts=[prompt],
        background=True,
    )

    base_url = request.host_url.rstrip("/")

    def event_stream():
        yield f"event: queued\ndata: {_json.dumps({'task_id': task.id, 'status': 'PENDING'})}\n\n"

        deadline = time.time() + 700
        last_status = None

        while time.time() < deadline:
            time.sleep(8)
            current = store.get_video_task(task.id)
            if not current:
                break

            if current.status != last_status:
                last_status = current.status

                if current.status == TaskStatus.PROCESSING:
                    yield f"event: processing\ndata: {_json.dumps({'task_id': task.id, 'status': 'PROCESSING'})}\n\n"

                elif current.status == TaskStatus.COMPLETED:
                    payload = {
                        "task_id": task.id,
                        "status": "COMPLETED",
                        "media_id": current.media_id,
                        "download_url": f"{base_url}/api/veo/download/{task.id}",
                    }
                    yield f"event: completed\ndata: {_json.dumps(payload)}\n\n"
                    return

                elif current.status == TaskStatus.FAILED:
                    payload = {
                        "task_id": task.id,
                        "status": "FAILED",
                        "error": current.error,
                    }
                    yield f"event: failed\ndata: {_json.dumps(payload)}\n\n"
                    return
            else:
                yield ": heartbeat\n\n"

        yield f"event: failed\ndata: {_json.dumps({'task_id': task.id, 'error': 'Timeout 700s'})}\n\n"

    return Response(
        event_stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/api-docs", methods=["GET"])
def api_docs_page():
    docs_path = os.path.join(ROOT_DIR, "api_docs.html")
    if os.path.exists(docs_path):
        return send_file(docs_path)
    return "API docs page not found", 404


@app.route("/api/docs", methods=["GET"])
def api_docs():
    return jsonify(
        {
            "title": "Custom Veo API Server",
            "version": "1.0.0",
            "base_url": "/api",
            "auth": "Bearer token in Authorization header",
            "endpoints": {
                "GET /api/me": "Get user profile",
                "GET /api/veo/projects": "List all projects",
                "POST /api/veo/project": "Create project {name}",
                "GET /api/veo/project/:id": "Get project detail",
                "DELETE /api/veo/project/:id": "Delete project",
                "GET /api/veo/video": "Get task status (?task_id=...) or list all",
                "POST /api/veo/text-to-video": "[Async] Text → Video, returns task_id",
                "POST /api/veo/image-to-video": "[Async] Image + Text → Video",
                "POST /api/veo/frames-to-video": "[Async] Start+End frames → Video",
                "GET /api/veo/download/<task_id>": "[KEY] Download video file when COMPLETED",
                "POST /api/veo/wait-for-video": "[Blocking] Create + wait, returns task info + download_url",
                "POST /api/veo/generate": "[Async] Submit + return task_id + poll_url + download_url",
                "POST /api/veo/create": "[★ SSE] 1 call - stream progress until done",
                "POST /api/admin/reload-cookie": "Reload Google cookie without restart",
            },
        }
    )


@app.route("/api/admin/reload-cookie", methods=["POST"])
@require_api_key
def reload_cookie():
    global _veo_service
    import json as _json

    body = request.get_json(silent=True) or {}
    new_cookie = body.get("cookie", "").strip()

    try:
        from core.veo_client import VeoClient

        if new_cookie:
            cookie = new_cookie
        else:
            cookie_path = os.path.join(ROOT_DIR, "cookies.json")
            if not os.path.exists(cookie_path):
                raise VietAutoAPIError("cookies.json not found", 404)
            with open(cookie_path, "r") as f:
                cookie = _json.load(f)

        veo_client = VeoClient(cookie)
        _veo_service = VeoService(veo_client)

        token_preview = ""
        if veo_client.access_token:
            token_preview = veo_client.access_token[:20] + "..."

        return success(
            {
                "message": "Cookie reloaded successfully. VeoClient reinitialized.",
                "token_ok": bool(veo_client.access_token),
                "token_preview": token_preview,
            }
        )

    except VietAutoAPIError:
        raise
    except Exception as e:
        logger.exception(f"[ReloadCookie] Error: {e}")
        raise VietAutoAPIError(f"Failed to reload cookie: {str(e)}", 500)


@app.route("/api/admin/settings/global-proxy", methods=["GET"])
@require_api_key
@require_role(UserRole.ADMIN)
def get_global_proxy():
    value = store.get_setting("global_proxy", "")
    return success({"global_proxy": value})


@app.route("/api/admin/settings/global-proxy", methods=["PUT"])
@require_api_key
@require_role(UserRole.ADMIN)
def update_global_proxy():
    body = request.get_json(silent=True) or {}
    proxy = body.get("global_proxy", "").strip()
    store.set_setting("global_proxy", proxy)
    return success({"global_proxy": proxy, "message": "Đã cập nhật Global Rotating Proxy thành công!"})


@app.route("/api/admin/settings/kiotproxy", methods=["GET"])
@require_api_key
@require_role(UserRole.ADMIN)
def get_kiotproxy_settings():
    svc = get_veo_service()
    status = svc.get_kiotproxy_status() if svc else {
        "key_configured": False,
        "key_preview": "",
        "region": "random",
        "current_proxy": None,
    }
    key = store.get_setting("kiotproxy_key", "")
    status["key"] = key
    return success(status)


@app.route("/api/admin/settings/kiotproxy", methods=["PUT"])
@require_api_key
@require_role(UserRole.ADMIN)
def update_kiotproxy_settings():
    body = request.get_json(silent=True) or {}
    key = body.get("key", "").strip()
    region = body.get("region", "random").strip()
    if region not in ("bac", "trung", "nam", "random"):
        region = "random"

    store.set_setting("kiotproxy_key", key)
    store.set_setting("kiotproxy_region", region)

    result = {"key": key, "region": region, "current_proxy": None, "message": "Đã lưu KiotProxy key."}
    if key:
        svc = get_veo_service()
        if svc:
            data = svc.get_kiotproxy_current()
            if data:
                result["current_proxy"] = {
                    "http": data.get("http"),
                    "host": data.get("host"),
                    "location": data.get("location"),
                    "ttl": data.get("ttl"),
                    "ttc": data.get("ttc"),
                }
                result["message"] = f"✅ KiotProxy hoạt động! IP: {data.get('http')} ({data.get('location')})"
            else:
                result["message"] = "⚠️ Đã lưu key nhưng không lấy được proxy. Kiểm tra lại key."

    return success(result)


@app.route("/api/admin/settings/kiotproxy/rotate", methods=["POST"])
@require_api_key
@require_role(UserRole.ADMIN)
def rotate_kiotproxy():
    svc = get_veo_service()
    if not svc:
        raise VietAutoAPIError("VeoService chưa khởi tạo", 500)

    data = svc.rotate_kiotproxy()
    if not data:
        raise VietAutoAPIError("Không thể đổi proxy. Key không hợp lệ hoặc chưa đến thời gian đổi (kiểm tra ttc).", 400)

    return success({
        "message": f"🔄 Đã đổi proxy mới: {data.get('http')} ({data.get('location')})",
        "current_proxy": {
            "http": data.get("http"),
            "socks5": data.get("socks5"),
            "host": data.get("host"),
            "location": data.get("location"),
            "ttl": data.get("ttl"),
            "ttc": data.get("ttc"),
            "realIpAddress": data.get("realIpAddress"),
        },
    })


@app.route("/api/admin/settings/kiotproxy/status", methods=["GET"])
@require_api_key
@require_role(UserRole.ADMIN)
def get_kiotproxy_status():
    svc = get_veo_service()
    if not svc:
        return success({"key_configured": False, "current_proxy": None})
    return success(svc.get_kiotproxy_status())


@app.route("/api/admin/settings/kiotproxy-pool", methods=["GET"])
def get_kiotproxy_pool():
    svc = get_veo_service()
    keys: list = []
    try:
        raw = store.get_setting("kiotproxy_keys", None)
        if isinstance(raw, list):
            keys = [str(k).strip() for k in raw if str(k or "").strip()]
        elif isinstance(raw, str) and raw.strip():
            keys = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    except Exception:
        pass
    if not keys:
        try:
            legacy = store.get_setting("kiotproxy_key", "")
            if legacy and str(legacy).strip():
                keys = [str(legacy).strip()]
        except Exception:
            pass
    try:
        m = int(store.get_setting("kiotproxy_accounts_per_key", 1) or 1)
    except Exception:
        m = 1
    m = max(1, m)
    try:
        region = store.get_setting("kiotproxy_region", "random") or "random"
    except Exception:
        region = "random"

    status = svc.get_kiotproxy_pool_status() if svc else {
        "keys_count": len(keys),
        "accounts_per_key": m,
        "region": region,
        "items": [],
        "unassigned_accounts": [],
    }
    return success({
        "keys": keys,  # raw, để user edit
        "accounts_per_key": m,
        "region": region,
        "status": status,
    })


@app.route("/api/admin/settings/kiotproxy-pool", methods=["PUT"])
@require_api_key
@require_role(UserRole.ADMIN)
def update_kiotproxy_pool():
    data = request.get_json(silent=True) or {}
    keys_raw = data.get("keys", [])
    if isinstance(keys_raw, str):
        keys = [ln.strip() for ln in keys_raw.splitlines() if ln.strip()]
    elif isinstance(keys_raw, list):
        keys = [str(k).strip() for k in keys_raw if str(k or "").strip()]
    else:
        keys = []
    seen = set()
    keys_dedup = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            keys_dedup.append(k)
    keys = keys_dedup

    try:
        m = int(data.get("accounts_per_key", 1) or 1)
    except Exception:
        m = 1
    m = max(1, m)
    region = str(data.get("region", "random") or "random").strip().lower()
    if region not in ("random", "bac", "trung", "nam"):
        region = "random"

    store.set_setting("kiotproxy_keys", keys)
    store.set_setting("kiotproxy_accounts_per_key", m)
    store.set_setting("kiotproxy_region", region)


    svc = get_veo_service()
    summary = svc.refresh_kiotproxy_pool(force=True) if svc else {"keys": len(keys)}
    status = svc.get_kiotproxy_pool_status() if svc else None
    return success({
        "message": f"Đã lưu {len(keys)} key · M={m} · region={region}.",
        "summary": summary,
        "status": status,
    })


@app.route("/api/admin/settings/kiotproxy-pool/refresh", methods=["POST"])
@require_api_key
@require_role(UserRole.ADMIN)
def refresh_kiotproxy_pool():
    svc = get_veo_service()
    if not svc:
        return error("VeoService chưa khởi tạo", 503)
    summary = svc.refresh_kiotproxy_pool(force=True)
    return success({
        "message": f"Refreshed pool. Fetched={summary['fetched']} Errors={summary['errors']}",
        "summary": summary,
        "status": svc.get_kiotproxy_pool_status(),
    })


@app.route("/api/admin/settings/kiotproxy-pool/status", methods=["GET"])
@require_api_key
@require_role(UserRole.ADMIN)
def get_kiotproxy_pool_status():
    svc = get_veo_service()
    if not svc:
        return success({"keys_count": 0, "items": []})
    return success(svc.get_kiotproxy_pool_status())


@app.route("/api/admin/settings/kiotproxy-pool/exclude-account", methods=["POST"])
@require_api_key
@require_role(UserRole.ADMIN)
def toggle_kiotproxy_exclude_account():
    body = request.get_json(silent=True) or {}
    name = str(body.get("account_name", "") or "").strip()
    if not name:
        return error("account_name bắt buộc", 400)
    excluded_flag = bool(body.get("excluded", True))

    try:
        raw = store.get_setting("kiotproxy_excluded_accounts", []) or []
    except Exception:
        raw = []
    cur = set(str(n).strip() for n in raw if str(n or "").strip()) if isinstance(raw, list) else set()

    if excluded_flag:
        cur.add(name)
        action = "excluded"
    else:
        cur.discard(name)
        action = "included"

    store.set_setting("kiotproxy_excluded_accounts", sorted(cur))

    svc = get_veo_service()
    status = svc.get_kiotproxy_pool_status() if svc else None
    return success({
        "message": (
            f"Đã bỏ {name} khỏi pool — account chạy direct (không proxy)"
            if excluded_flag else
            f"Đã thêm {name} trở lại pool"
        ),
        "excluded_count": len(cur),
        "excluded_accounts": sorted(cur),
        "status": status,
    })


@app.route("/api/admin/settings/nanoai-token", methods=["GET"])
def get_nanoai_token():
    token = store.get_setting("nanoai_token", "")
    return success({"nanoai_token": token or ""})


@app.route("/api/admin/settings/nanoai-token", methods=["PUT"])
@require_api_key
@require_role(UserRole.ADMIN)
def update_nanoai_token():
    body = request.get_json(silent=True) or {}
    token = body.get("nanoai_token", "").strip()
    store.set_setting("nanoai_token", token)
    return success({
        "nanoai_token": token,
        "message": "Đã lưu NanoAI API token thành công!" if token else "Đã xoá NanoAI token.",
    })


@app.route("/api/admin/settings/proxy-pool/import", methods=["POST"])
@require_api_key
@require_role(UserRole.ADMIN)
def import_proxy_pool():
    body = request.get_json(silent=True) or {}
    raw_text = body.get("proxy_list", "").strip()
    if not raw_text:
        raise ValidationError("proxy_list is required (mỗi dòng 1 proxy: host:port:user:pass)")

    proxies = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        normalized = _normalize_proxy(line)
        if normalized:
            proxies.append(normalized)

    if not proxies:
        raise ValidationError("Không tìm thấy proxy hợp lệ trong danh sách.")

    accounts = store.list_veo_accounts()
    if not accounts:
        raise VietAutoAPIError("Chưa có account nào trong hệ thống.", 404)

    assigned = []
    for i, acc in enumerate(accounts):
        proxy = proxies[i % len(proxies)]
        acc.proxy = proxy
        store.update_veo_account(acc)
        assigned.append({"name": acc.name, "proxy": proxy})


    return success({
        "message": f"Đã gán {len(proxies)} proxy cho {len(accounts)} accounts thành công!",
        "total_proxies": len(proxies),
        "total_accounts": len(accounts),
        "assignments": assigned,
    })


@app.route("/api/admin/settings/proxy-pool/clear", methods=["POST"])
@require_api_key
@require_role(UserRole.ADMIN)
def clear_proxy_pool():
    accounts = store.list_veo_accounts()
    cleared = 0
    for acc in accounts:
        if acc.proxy:
            acc.proxy = ""
            acc.static_proxy = ""
            store.update_veo_account(acc)
            cleared += 1

    return success({
        "message": f"Đã xoá proxy của {cleared} accounts. Tất cả sẽ dùng DIRECT (no proxy).",
        "cleared": cleared,
    })


@app.route("/api/admin/proxy-pool/reserve/upload", methods=["POST"])
@require_api_key
@require_role(UserRole.ADMIN)
def upload_proxy_reserve_pool():
    body = request.get_json(silent=True) or {}
    raw_text = body.get("proxy_list", "").strip()
    mode = body.get("mode", "replace").strip().lower()

    if not raw_text:
        raise ValidationError("proxy_list is required (mỗi dòng 1 proxy: host:port:user:pass)")

    check_live = body.get("check_live", True)  # Mặc định check TCP connectivity

    proxies = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        normalized = _normalize_proxy(line)
        if normalized:
            proxies.append(normalized)

    if not proxies:
        raise ValidationError("Không tìm thấy proxy hợp lệ trong danh sách.")

    svc = get_veo_service()
    result = svc.validate_and_set_proxy_pool(proxies, mode=mode, check_live=check_live)


    parts = [f"Đã thêm {result['added']}/{len(proxies)} proxy vào pool dự phòng"]
    if result["skipped_dead"] > 0:
        parts.append(f"bỏ qua {result['skipped_dead']} proxy đã từng bị chặn")
    if result["skipped_offline"] > 0:
        parts.append(f"bỏ qua {result['skipped_offline']} proxy offline")

    return success({
        "message": " | ".join(parts),
        "uploaded_raw": len(proxies),
        "added": result["added"],
        "skipped_dead": result["skipped_dead"],
        "skipped_offline": result["skipped_offline"],
        "pool_total": result["total"],
        "mode": mode,
    })


@app.route("/api/admin/proxy-pool/reserve", methods=["GET"])
@require_api_key
@require_role(UserRole.ADMIN)
def get_proxy_reserve_pool():
    svc = get_veo_service()
    pool = svc.get_proxy_reserve_pool()
    dead_log = svc.get_dead_proxy_log()

    return success({
        "pool": pool,
        "pool_count": len(pool),
        "dead_log": dead_log,
        "dead_count": len(dead_log),
    })


@app.route("/api/admin/proxy-pool/reserve/clear", methods=["DELETE", "POST"])
@require_api_key
@require_role(UserRole.ADMIN)
def clear_proxy_reserve_pool():
    svc = get_veo_service()
    removed = svc.clear_proxy_reserve_pool()

    return success({
        "message": f"Đã xoá {removed} proxy dự phòng khỏi pool.",
        "removed": removed,
    })


from web.models import VeoAccount

@app.route("/api/veo/accounts", methods=["GET"])
@require_api_key
def get_accounts():
    accounts = store.list_veo_accounts()
    users = {u.id: u.username for u in store.list_users()}

    mem_busy: dict = {}  # {account_name: task_count}
    try:
        svc = _veo_service  # singleton toàn cục, None nếu chưa khởi tạo
        if svc is not None:
            with svc._cookie_cond:
                for acct_name, cnt in getattr(svc, "_cookie_busy_veo", {}).items():
                    if cnt and cnt > 0:
                        mem_busy[acct_name] = mem_busy.get(acct_name, 0) + int(cnt)

    except Exception:
        pass

    account_project_map: dict = {}
    try:
        processing_tasks, _ = store.list_video_tasks(status="PROCESSING", limit=500)
        for t in processing_tasks:
            acct_name = getattr(t, "picked_account_name", None)
            proj_id = getattr(t, "project_id", None)
            if acct_name:
                if acct_name not in account_project_map:
                    account_project_map[acct_name] = []
                if proj_id:
                    _proj = store.get_project(proj_id)
                    proj_name = _proj.name if _proj and _proj.name else proj_id
                    if proj_name not in account_project_map[acct_name]:
                        account_project_map[acct_name].append(proj_name)
    except Exception:
        pass

    result = []
    _blacklist = set()
    _ban_reasons = {}
    try:
        svc = _veo_service
        if svc is not None:
            _blacklist = getattr(svc, '_account_project_blacklist', set())
            _ban_reasons = svc.get_ban_reasons() if hasattr(svc, 'get_ban_reasons') else {}
    except Exception:
        pass
    assignment_map = store.get_veo_assignment_map()
    owner_by_account = {
        account_id: user_id
        for user_id, account_ids in assignment_map.items()
        for account_id in account_ids
    }
    for a in accounts:
        d = a.to_dict()
        assigned_user_id = owner_by_account.get(a.id) or a.assigned_to_user_id
        d["assigned_to_user_id"] = assigned_user_id
        d["assigned_to_username"] = users.get(assigned_user_id) if assigned_user_id else None
        try:
            d["health"] = _decode_cookie_health(a.cookie)
        except Exception:
            d["health"] = {"status": "check_failed", "days_left": None, "expires_at": None}
        d["project_blacklisted"] = a.name in _blacklist
        if not a.is_active:
            d["ban_reason"] = _ban_reasons.get(a.name, "") or getattr(a, "ban_reason", "") or ""
        else:
            d["ban_reason"] = ""
        mem_count = mem_busy.get(a.name, 0)
        db_projects = account_project_map.get(a.name, [])
        d["active_tasks_count"] = mem_count  # in-memory = chính xác nhất
        d["active_projects"] = db_projects   # project IDs từ DB (video tasks)
        result.append(d)
    return success(result)


@app.route("/api/admin/account-tokens/reset", methods=["POST"])
@require_api_key
@require_role(UserRole.ADMIN)
def reset_account_token_statuses():
    result = store._veo_accounts.update_many({}, {"$unset": {"api_session": ""}})
    return success({
        "matched": result.matched_count,
        "modified": result.modified_count,
        "message": "Reset account token statuses to unused",
    })


def _normalize_proxy(raw: str):
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None

    lower = s.lower()
    for scheme in ("http://", "https://", "socks5://", "socks4://", "socks4a://"):
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

    return s


@app.route("/api/veo/accounts", methods=["POST"])
@require_api_key
def create_account():

    body = request.get_json(silent=True) or {}
    validate_required(body, "name", "cookie")

    raw_proxy = body.get("proxy", "").strip()
    raw_static_proxy = body.get("static_proxy", "").strip()
    acc = VeoAccount(
        id=str(uuid.uuid4()),
        name=body["name"].strip(),
        cookie=body["cookie"].strip(),
        proxy=_normalize_proxy(raw_proxy),
        static_proxy=_normalize_proxy(raw_static_proxy),
        is_active=bool(body.get("is_active", True))
    )
    store.create_veo_account(acc)
    store.restore_deleted_account(acc.name)
    return success(acc.to_dict(), 201)

@app.route("/api/veo/accounts/<account_id>", methods=["PUT"])
@require_api_key
def update_account(account_id):
    acc = store.get_veo_account(account_id)
    if not acc:
        raise VietAutoAPIError("Account not found", 404)

    body = request.get_json(silent=True) or {}
    if "name" in body and body["name"] is not None:
        acc.name = body["name"].strip()
    if "cookie" in body and body["cookie"] is not None:
        acc.cookie = body["cookie"].strip()
    if "proxy" in body:
        acc.proxy = _normalize_proxy((body["proxy"] or "").strip())
    if "static_proxy" in body:
        acc.static_proxy = _normalize_proxy((body["static_proxy"] or "").strip())
    if "is_active" in body:
        was_inactive = not bool(getattr(acc, "is_active", False))
        acc.is_active = bool(body["is_active"])
        if acc.is_active:
            acc.ban_reason = None
            try:
                svc = _veo_service
                if svc:
                    if hasattr(svc, '_ban_reasons'):
                        svc._ban_reasons.pop(acc.name, None)
                    if hasattr(svc, '_account_project_blacklist'):
                        svc._account_project_blacklist.discard(acc.name)
                    for attr in (
                        "_account_daily_quota_exhausted",
                        "_account_403_fails",
                        "_account_ip_errors",
                        "_unusual_activity_counts",
                    ):
                        mp = getattr(svc, attr, None)
                        if isinstance(mp, dict):
                            mp.pop(acc.name, None)
                    if hasattr(svc, "_per_account_task_ts"):
                        svc._per_account_task_ts.pop(acc.name, None)
                    if was_inactive:
                        pass
            except Exception as _e:
                pass

    store.update_veo_account(acc)

    if "cookie" in body and body["cookie"] is not None:
        veo = get_veo_service()
        if veo and hasattr(veo, "_gl_project_cache"):
            _keys_to_clear = [
                k for k in list(veo._gl_project_cache.keys())
                if k[0] == acc.name
            ]
            for _k in _keys_to_clear:
                veo._gl_project_cache.pop(_k, None)
            if _keys_to_clear:
                pass

    return success(acc.to_dict())


def _cleanup_account_memory(acc_name: str):
    try:
        svc = _veo_service
        if svc:
            if hasattr(svc, '_ban_reasons'):
                svc._ban_reasons.pop(acc_name, None)
            if hasattr(svc, '_cookie_busy_veo'):
                svc._cookie_busy_veo.pop(acc_name, None)
            if hasattr(svc, '_gl_project_cache'):
                keys_to_clear = [k for k in list(svc._gl_project_cache.keys()) if k[0] == acc_name]
                for k in keys_to_clear:
                    svc._gl_project_cache.pop(k, None)
            if hasattr(svc, '_account_project_blacklist'):
                svc._account_project_blacklist.discard(acc_name)
    except Exception:
        pass

    _last_cookie_sync.pop(acc_name, None)


@app.route("/api/veo/accounts/<account_id>", methods=["DELETE"])
@require_api_key
@require_role(UserRole.ADMIN)
def delete_veo_account(account_id):
    acc = store.get_veo_account(account_id)
    if not acc:
        raise VietAutoAPIError("Account not found", 404)

    acc_name = acc.name
    deleted = store.delete_veo_account(account_id)
    if not deleted:
        raise VietAutoAPIError("Không xoá được account", 500)

    _cleanup_account_memory(acc_name)

    return success({"message": f"Đã xoá account '{acc_name}' thành công. Extension sẽ không tự tạo lại."})


@app.route("/api/veo/accounts/bulk-delete", methods=["POST"])
@require_api_key
@require_role(UserRole.ADMIN)
def bulk_delete_veo_accounts():
    body = request.get_json(silent=True) or {}
    ids = body.get("ids", [])
    if not ids or not isinstance(ids, list):
        raise VietAutoAPIError("Thiếu danh sách ids cần xoá", 400)

    accounts = store.list_veo_accounts()
    name_map = {a.id: a.name for a in accounts if a.id in ids}

    deleted = store.bulk_delete_veo_accounts(ids)

    for acc_id in ids:
        acc_name = name_map.get(acc_id)
        if acc_name:
            _cleanup_account_memory(acc_name)

    return success({
        "message": f"Đã xoá {deleted}/{len(ids)} tài khoản thành công. Extension sẽ không tự tạo lại.",
        "deleted": deleted,
        "requested": len(ids),
    })


@app.route("/api/veo/accounts/bulk-unban", methods=["POST"])
@require_api_key
@require_role(UserRole.ADMIN)
def bulk_unban_veo_accounts():
    body = request.get_json(silent=True) or {}
    ids = body.get("ids", [])
    if not ids or not isinstance(ids, list):
        raise VietAutoAPIError("Thiếu danh sách ids cần gỡ ban", 400)

    unbanned = store.bulk_unban_veo_accounts(ids)

    try:
        svc = _veo_service
        if svc and hasattr(svc, '_ban_reasons'):
            accounts = store.list_veo_accounts()
            name_map = {a.id: a.name for a in accounts if a.id in ids}
            for acc_id in ids:
                acc_name = name_map.get(acc_id)
                if acc_name:
                    svc._ban_reasons.pop(acc_name, None)
    except Exception:
        pass

    return success({
        "message": f"Đã gỡ ban {unbanned}/{len(ids)} tài khoản thành công.",
        "unbanned": unbanned,
        "requested": len(ids),
    })


@app.route("/api/veo/accounts/deleted-blacklist", methods=["GET"])
@require_api_key
@require_role(UserRole.ADMIN)
def get_deleted_blacklist():
    names = store.list_deleted_accounts()
    return success({"deleted_accounts": names, "count": len(names)})


@app.route("/api/veo/accounts/deleted-blacklist/clear", methods=["POST"])
@require_api_key
@require_role(UserRole.ADMIN)
def clear_deleted_blacklist():
    names = store.list_deleted_accounts()
    for name in names:
        store.restore_deleted_account(name)
    return success({"message": f"Đã xoá blacklist {len(names)} accounts. Extension sẽ có thể tạo lại.", "cleared": len(names)})


@app.route("/api/veo/accounts/deleted-blacklist/restore", methods=["POST"])
@require_api_key
@require_role(UserRole.ADMIN)
def restore_from_blacklist():
    body = request.get_json(silent=True) or {}
    names = body.get("names", [])
    if not names or not isinstance(names, list):
        raise VietAutoAPIError("Thiếu danh sách names cần gỡ", 400)
    restored = 0
    for name in names:
        store.restore_deleted_account(name)
        restored += 1
    return success({"message": f"Đã gỡ {restored} accounts khỏi blacklist.", "restored": restored})


@app.route("/api/cookie-sync", methods=["POST"])
def auto_update_cookie():
    import uuid
    body = request.get_json(silent=True) or {}
    account_name = body.get("account")
    if not account_name:
        return jsonify({"error": "account required"}), 400

    veo_cookie = body.get("veo_cookie")

    if not veo_cookie:
        return jsonify({"error": "No cookies provided"}), 400

    accounts = store.list_veo_accounts()
    matched = None
    for a in accounts:
        if a.name == account_name or account_name in (a.name or ""):
            matched = a
            break

    if not matched:
        if store.is_account_deleted(account_name):
            return jsonify({"ok": False, "action": "blocked", "reason": "Account đã bị xoá. Không tự tạo lại."}), 200

        matched = VeoAccount(
            id=str(uuid.uuid4()),
            name=account_name,
            cookie=veo_cookie or "",
        )
        store.create_veo_account(matched)
        return jsonify({"ok": True, "action": "created", "account_id": matched.id})

    updated_fields = []
    is_cookie_changed = False
    
    if veo_cookie and veo_cookie != matched.cookie:
        matched.cookie = veo_cookie
        updated_fields.append("veo_cookie")
        is_cookie_changed = True
        

    store.update_veo_account(matched)

    if is_cookie_changed:
        veo = get_veo_service()
        if veo:
            if hasattr(veo, "_gl_project_cache"):
                _keys_to_clear = [
                    k for k in list(veo._gl_project_cache.keys())
                    if k[0] == matched.name
                ]
                for _k in _keys_to_clear:
                    veo._gl_project_cache.pop(_k, None)
            
            if hasattr(veo, "_account_project_blacklist"):
                veo._account_project_blacklist.discard(matched.name)


    import time as _t
    _last_cookie_sync[matched.name] = _t.time()


    return jsonify({"ok": True, "action": "updated", "fields": updated_fields})


@app.route("/api/veo/accounts/<account_id>/unblock", methods=["POST"])
@require_api_key
def unblock_account(account_id):
    acc = store.get_veo_account(account_id)
    if not acc:
        raise VietAutoAPIError("Account not found", 404)
    veo = get_veo_service()
    if veo and hasattr(veo, '_account_project_blacklist'):
        veo._account_project_blacklist.discard(acc.name)
    return success({"message": f"Đã gỡ chặn account {acc.name}", "account_name": acc.name})

@app.route("/api/veo/accounts/<account_id>/assign", methods=["POST"])
@require_api_key
@require_role(UserRole.ADMIN)
def assign_account_to_user(account_id):
    acc = store.get_veo_account(account_id)
    if not acc:
        raise VietAutoAPIError("Account not found", 404)
    body = request.get_json(silent=True) or {}
    username = body.get("username", "").strip()
    if not username:
        raise ValidationError("username is required")
    user = store.get_user_by_username(username)
    if not user:
        raise VietAutoAPIError(f"User '{username}' not found", 404)
    acc.assigned_to_user_id = user.id
    store.update_veo_account(acc)
    if not user.default_veo_cookie:
        user.default_veo_cookie = acc.cookie
        store.update_user(user)
    return success({"account_id": account_id, "assigned_to": user.username})

@app.route("/api/veo/accounts/<account_id>/assign", methods=["DELETE"])
@require_api_key
@require_role(UserRole.ADMIN)
def unassign_account(account_id):
    acc = store.get_veo_account(account_id)
    if not acc:
        raise VietAutoAPIError("Account not found", 404)
    prev_user_id = acc.assigned_to_user_id
    store.unassign_veo_account(account_id)
    if prev_user_id:
        user = store.get_user(prev_user_id)
        if user and user.default_veo_cookie == acc.cookie:
            user.default_veo_cookie = None
            store.update_user(user)
    return success({"account_id": account_id, "status": "free"})


def _decode_cookie_health(cookie_str: str) -> dict:
    import base64, json as _json

    def _calc_status(exp_ts):
        now = int(time.time())
        if now > exp_ts:
            return {"status": "expired", "days_left": 0, "expires_at": exp_ts}
        days_left = max(0, int((exp_ts - now) / 86400))
        status = "warning" if days_left <= 3 else "ok"
        return {"status": status, "days_left": days_left, "expires_at": exp_ts}

    def _decode_jwt_exp(token: str):
        try:
            parts = token.strip().split(".")
            if len(parts) < 3:  # JWE có 5 parts, JWT có 3
                return None
            payload_b64 = parts[1]
            payload_b64 += "=" * (4 - len(payload_b64) % 4)
            payload = _json.loads(base64.urlsafe_b64decode(payload_b64))
            return int(payload["exp"]) if payload.get("exp") else None
        except Exception:
            return None

    stripped = cookie_str.strip() if cookie_str else ""
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            cookies = _json.loads(stripped)
            if isinstance(cookies, dict):
                cookies = [cookies]
            session_cookie = None
            for c in cookies:
                name = c.get("name", "")
                if "__Secure-next-auth.session-token" in name or "session-token" in name:
                    session_cookie = c
                    break

            if not session_cookie:
                return {"status": "no_token", "days_left": None, "expires_at": None}

            exp_date = session_cookie.get("expirationDate") or session_cookie.get("expiration")
            if exp_date:
                exp_ts = int(float(exp_date))
                if exp_ts > 1_000_000_000_000:
                    exp_ts = exp_ts // 1000
                return _calc_status(exp_ts)

            jwt_exp = _decode_jwt_exp(session_cookie.get("value", ""))
            if jwt_exp:
                return _calc_status(jwt_exp)
            return {"status": "ok", "days_left": None, "expires_at": None}

        except Exception as e:
            pass

    session_token = None
    for part in cookie_str.split(";"):
        part = part.strip()
        if "__Secure-next-auth.session-token=" in part or "session-token=" in part:
            session_token = part.split("=", 1)[1].strip()
            break

    if not session_token:
        return {"status": "no_token", "days_left": None, "expires_at": None}

    return {"status": "ok_unverified", "days_left": None, "expires_at": None}


def _check_cookie_full(cookie_str: str, proxy: str = None) -> dict:
    import json as _json

    offline = _decode_cookie_health(cookie_str)
    if offline["status"] in ("expired", "no_token"):
        return offline

    cookie_to_use = cookie_str
    stripped = (cookie_str or "").strip()
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            cookies_list = _json.loads(stripped)
            if isinstance(cookies_list, dict):
                cookies_list = [cookies_list]
            parts = [f"{c['name']}={c['value']}" for c in cookies_list if c.get("name") and c.get("value")]
            cookie_to_use = "; ".join(parts)
        except Exception:
            pass

    try:
        from core.veo_client import VeoClient
        client = VeoClient(cookie=cookie_to_use, proxy=proxy)
        if not client.access_token:
            client.get_session_token()
        if not client.access_token:
            return {"status": "revoked", "days_left": offline.get("days_left"), "expires_at": offline.get("expires_at")}

        try:
            from core.project import search_user_projects
            projects = search_user_projects(
                cookie=cookie_to_use,
                access_token=client.access_token,
                proxy=proxy,
            )
            if projects is None:
                pass
        except Exception as proj_e:
            pass

        return offline
    except Exception as e:
        return {
            "status": "check_failed",
            "days_left": offline.get("days_left"),
            "expires_at": offline.get("expires_at"),
            "error": f"{type(e).__name__}: {str(e)[:100]}",
        }


@app.route("/api/veo/accounts/<account_id>/check", methods=["POST"])
@require_api_key
def check_account_health(account_id):
    acc = store.get_veo_account(account_id)
    if not acc:
        raise VietAutoAPIError("Account not found", 404)
    result = _check_cookie_full(acc.cookie, proxy=acc.proxy)
    return success({"id": account_id, "name": acc.name, **result})


@app.route("/api/veo/accounts/check-all", methods=["POST"])
@require_api_key
def check_all_accounts_health():
    accounts = store.list_veo_accounts()
    results = []
    for acc in accounts:
        health = _check_cookie_full(acc.cookie, proxy=acc.proxy)
        results.append({"id": acc.id, "name": acc.name, **health})
    return success(results)


def _check_account_credits(cookie_str: str, proxy: str = None) -> dict:
    import json as _json
    import requests

    CREDITS_URL = "https://aisandbox-pa.googleapis.com/v1/credits"
    API_KEY = "AIzaSyBtrm0o5ab1c-Ec8ZuLcGt3oJAA5VWt3pY"

    cookie_to_use = cookie_str
    stripped = (cookie_str or "").strip()
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            cookies_list = _json.loads(stripped)
            if isinstance(cookies_list, dict):
                cookies_list = [cookies_list]
            parts = [f"{c['name']}={c['value']}" for c in cookies_list if c.get("name") and c.get("value")]
            cookie_to_use = "; ".join(parts)
        except Exception:
            pass

    try:
        from core.veo_client import VeoClient
        client = VeoClient(cookie=cookie_to_use, proxy=proxy)
        if not client.access_token:
            client.get_session_token()
        if not client.access_token:
            return {"credits": None, "error": "no_access_token"}
    except Exception as e:
        return {"credits": None, "error": f"auth_failed: {str(e)[:100]}"}

    try:
        from core import browser_config as bcfg
        headers = {
            "accept": "*/*",
            "authorization": f"Bearer {client.access_token}",
            "origin": "https://labs.google",
            "referer": "https://labs.google/",
            "user-agent": bcfg.get(
                "user_agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            ),
            "x-browser-channel": bcfg.get("x_browser_channel", "stable"),
            "x-browser-validation": bcfg.get("x_browser_validation"),
            "x-browser-year": bcfg.get("x_browser_year", "2026"),
            "x-client-data": bcfg.get("x_client_data"),
        }
        headers = {k: v for k, v in headers.items() if v}

        proxies = {"http": proxy, "https": proxy} if proxy else None
        resp = requests.get(
            f"{CREDITS_URL}?key={API_KEY}",
            headers=headers,
            proxies=proxies,
            timeout=15,
            verify=False,
        )

        if resp.status_code != 200:
            return {"credits": None, "error": f"api_error_{resp.status_code}"}

        data = resp.json()
        return {"credits_data": data, "error": None}
    except Exception as e:
        return {"credits": None, "error": f"request_failed: {str(e)[:100]}"}


@app.route("/api/veo/accounts/<account_id>/credits", methods=["POST"])
@require_api_key
def check_account_credits(account_id):
    acc = store.get_veo_account(account_id)
    if not acc:
        raise VietAutoAPIError("Account not found", 404)
    result = _check_account_credits(acc.cookie, proxy=acc.proxy)
    return success({"id": account_id, "name": acc.name, **result})


@app.route("/api/veo/accounts/check-credits", methods=["POST"])
@require_api_key
@require_role(UserRole.ADMIN)
def check_all_credits():
    accounts = store.list_veo_accounts()
    results = []
    for acc in accounts:
        credit_info = _check_account_credits(acc.cookie, proxy=acc.proxy)
        results.append({"id": acc.id, "name": acc.name, **credit_info})
    return success(results)


@app.route("/api/veo/accounts/check-proxy", methods=["POST"])
@require_api_key
@require_role(UserRole.ADMIN)
def check_all_proxy_health():
    import time as _time
    accounts = store.list_veo_accounts()
    results = []

    for acc in accounts:
        if not acc.proxy:
            results.append({"id": acc.id, "name": acc.name, "proxy": None, "alive": None, "latency_ms": None, "error": "no_proxy"})
            continue

        proxy_url = acc.proxy.strip()
        start = _time.time()
        try:
            import requests as _req_proxy
            resp = _req_proxy.get(
                "https://httpbin.org/ip",
                proxies={"http": proxy_url, "https": proxy_url},
                timeout=10,
                verify=False,
            )
            elapsed_ms = int((_time.time() - start) * 1000)
            if resp.status_code == 200:
                data = resp.json()
                results.append({
                    "id": acc.id,
                    "name": acc.name,
                    "proxy": proxy_url,
                    "alive": True,
                    "latency_ms": elapsed_ms,
                    "ip": data.get("origin", ""),
                    "error": None,
                })
            else:
                results.append({
                    "id": acc.id, "name": acc.name, "proxy": proxy_url,
                    "alive": False, "latency_ms": elapsed_ms,
                    "ip": None, "error": f"HTTP {resp.status_code}",
                })
        except Exception as e:
            elapsed_ms = int((_time.time() - start) * 1000)
            err_msg = str(e)
            if len(err_msg) > 80:
                err_msg = err_msg[:80] + "..."
            results.append({
                "id": acc.id, "name": acc.name, "proxy": proxy_url,
                "alive": False, "latency_ms": elapsed_ms,
                "ip": None, "error": err_msg,
            })

    alive_count = sum(1 for r in results if r.get("alive"))
    return success({"results": results, "alive": alive_count, "total": len(results)})


@app.route("/output.css")
def serve_css():
    from flask import send_from_directory, make_response
    resp = make_response(send_from_directory(SPA_BUILD_DIR, "output.css"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_spa(path):
    if path.startswith("api/"):
        return jsonify({"success": False, "error": "API route not found"}), 404

    import os
    from flask import send_from_directory, make_response

    if path and os.path.exists(os.path.join(SPA_BUILD_DIR, path)):
        return send_from_directory(SPA_BUILD_DIR, path)

    resp = make_response(send_file(os.path.join(SPA_BUILD_DIR, "index.html")))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/api/veo/accounts/<account_id>/launch-chrome", methods=["POST"])
@require_api_key
@require_role(UserRole.ADMIN)
def launch_chrome_for_account(account_id):
    try:
        from web.chrome_launcher import launch_chrome
        acc = store.get_veo_account(account_id)
        if not acc:
            return jsonify({"error": "Account không tồn tại"}), 404
        account_email = acc.name  # VeoAccount.name là email
        result = launch_chrome(account_email=account_email)
        if result["success"]:
            return jsonify({"success": True, "pid": result.get("pid"), "account": account_email})
        return jsonify({"success": False, "error": result.get("error")}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/veo/accounts/<account_id>/close-chrome", methods=["POST"])
@require_api_key
@require_role(UserRole.ADMIN)
def close_chrome_for_account(account_id):
    try:
        from web.chrome_launcher import close_chrome
        acc = store.get_veo_account(account_id)
        if not acc:
            return jsonify({"error": "Account không tồn tại"}), 404
        result = close_chrome(account_email=acc.name)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/captcha/status", methods=["GET"])
@require_api_key
def captcha_status():
    try:
        from core.captcha_server import get_connected_accounts, _server_started
        from web.chrome_launcher import get_running_accounts
        return jsonify({
            "server_running": _server_started,
            "connected_accounts": get_connected_accounts(),
            "chrome_processes": get_running_accounts(),
        })
    except Exception as e:
        return jsonify({"server_running": False, "connected_accounts": [], "error": str(e)})


_last_cookie_sync: dict = {}

_PHASE_LABELS = {
    "RESOLVING_COOKIE": "🍪 Đang lấy Cookie",
    "SOLVING_CAPTCHA_EXT": "🧩 Captcha (Extension)",
    "SOLVING_CAPTCHA_SELF": "🧩 Captcha (Self)",
    "SOLVING_CAPTCHA_REMOTE": "🌐 Captcha (Remote Server)",
    "CALLING_API": "🔗 Gọi API Google",
    "POLLING_RESULT": "⏳ Chờ kết quả",
    "DOWNLOADING": "⬇️ Đang tải",
}


@app.route("/api/veo/accounts/activity", methods=["GET"])
@require_api_key
@require_role(UserRole.ADMIN)
def get_accounts_activity():
    import time as _time

    accounts = store.list_veo_accounts()

    activities = {}
    ban_reasons = {}
    proxy_health = {}
    ip_errors = {}
    try:
        svc = _veo_service
        if svc:
            svc._cleanup_stale_activities()
            activities = svc.get_account_activities()
            ban_reasons = svc.get_ban_reasons()
            proxy_health = svc.get_proxy_health()
            ip_errors = svc.get_ip_error_counts()
    except Exception:
        pass

    captcha_connected = set()
    captcha_workers_list = []  # worker_id entries
    try:
        from core.captcha_server import get_connected_accounts, _registered, _lock, HEARTBEAT_TIMEOUT
        all_connected = get_connected_accounts()
        captcha_connected = set(all_connected)
        for name in all_connected:
            if name.startswith("worker_"):
                with _lock:
                    ts = _registered.get(name, 0)
                captcha_workers_list.append({
                    "name": name,
                    "connected_since": ts,
                })
    except Exception:
        pass

    now = _time.time()
    result = []

    for a in accounts:
        act = activities.get(a.name)

        if act:
            phase = act["phase"]
            phase_label = _PHASE_LABELS.get(phase, phase)
            task_id = act.get("task_id", "")
            duration = int(now - act.get("since", now))
        else:
            phase = None
            phase_label = "✅ Rảnh"
            task_id = ""
            duration = 0

        last_sync_ts = _last_cookie_sync.get(a.name, 0)
        cookie_sync_recent = (now - last_sync_ts) < 180  # 3 phút

        result.append({
            "id": a.id,
            "name": a.name,
            "is_active": a.is_active,
            "current_phase": phase,
            "phase_label": phase_label,
            "task_id": task_id,
            "duration_seconds": duration,
            "captcha_connected": a.name in captcha_connected,
            "extension_role": "cookie" if cookie_sync_recent else "none",
            "entry_type": "account",
            "ban_reason": ban_reasons.get(a.name, "") if not a.is_active else "",
            "proxy": a.proxy or "",
            "static_proxy": a.static_proxy or "",
            "proxy_status": proxy_health.get(a.name, "unknown"),
            "ip_error_count": ip_errors.get(a.name, 0),
        })

    try:
        from core.captcha_server import _worker_activity, _lock as _cs_lock
        _w_activity = {}
        with _cs_lock:
            _stale_workers = []
            for _w_id, _w_info in _worker_activity.items():
                if _w_info.get("phase") == "SOLVING" and (now - _w_info.get("since", now)) > 60:
                    _stale_workers.append(_w_id)
            for _w_id in _stale_workers:
                _worker_activity[_w_id] = {"phase": "IDLE", "request_id": "", "since": now}
            _w_activity = dict(_worker_activity)
    except Exception:
        _w_activity = {}

    for w in captcha_workers_list:
        w_name = w["name"]
        w_duration = int(now - w["connected_since"]) if w["connected_since"] else 0

        w_act = _w_activity.get(w_name)
        if w_act and w_act.get("phase") == "SOLVING":
            w_phase = "CAPTCHA_SOLVING"
            w_phase_label = "🔄 Đang giải captcha"
            w_req_id = w_act.get("request_id", "")
            w_duration = int(now - w_act.get("since", now))
        else:
            w_phase = "CAPTCHA_IDLE"
            w_phase_label = "🧩 Đang chờ job"
            w_req_id = ""

        result.append({
            "name": w_name,
            "is_active": True,
            "current_phase": w_phase,
            "phase_label": w_phase_label,
            "task_id": w_req_id,
            "duration_seconds": w_duration,
            "captcha_connected": True,
            "extension_role": "captcha",
            "entry_type": "captcha_worker",
        })

    return success({
        "accounts": result,
        "captcha_workers_online": len(captcha_workers_list),
    })


@app.route("/api/veo/accounts/captcha-stats", methods=["GET"])
@require_api_key
@require_role(UserRole.ADMIN)
def get_captcha_stats():
    veo = get_veo_service()
    if not veo:
        return success({"per_account": {}, "totals": {"remote_received": 0, "remote_pass": 0, "remote_fail": 0, "extension_received": 0, "extension_pass": 0, "extension_fail": 0}})

    per_account = veo.get_captcha_stats()

    accounts = store.list_veo_accounts()
    acc_id_map = {a.name: a.id for a in accounts}
    acc_active_map = {a.name: a.is_active for a in accounts}

    for acc_name, stats in per_account.items():
        stats["account_id"] = acc_id_map.get(acc_name, "")
        stats["is_active"] = acc_active_map.get(acc_name, True)

    totals = {
        "remote_received": 0, "remote_pass": 0, "remote_fail": 0,
        "extension_received": 0, "extension_pass": 0, "extension_fail": 0,
    }
    for stats in per_account.values():
        for key in totals:
            totals[key] += stats.get(key, 0)

    return success({
        "per_account": per_account,
        "totals": totals,
    })


@app.route("/api/veo/queue/settings", methods=["GET"])
def queue_settings_get():
    veo = get_veo_service()
    if not veo:
        return jsonify({"success": True, "data": {"max_concurrent": 0, "queue_pending": 0, "active_workers": 0}})
    qs = veo.get_queue_status()
    return jsonify({
        "success": True,
        "data": qs,
    })


@app.route("/api/admin/settings/queue_advanced", methods=["PUT"])
@require_api_key
def queue_settings_update():
    body = request.get_json(silent=True) or {}
    new_max = body.get("max_concurrent")
    if new_max is None:
        return jsonify({"success": False, "error": "Thiếu field 'max_concurrent'"}), 400
    new_max = max(1, int(new_max))
    veo = get_veo_service()
    if not veo:
        return jsonify({"success": False, "error": "VeoService chưa khởi tạo"}), 500
    old_veo = veo._max_concurrent
    veo.update_max_concurrent(new_max)
    return jsonify({
        "success": True,
        "data": {
            "max_concurrent": veo._max_concurrent,
        }
    })


@app.after_request
def _add_captcha_cors(response):
    if request.path.startswith('/captcha/'):
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response

@app.route("/captcha/register", methods=["POST", "OPTIONS"])
def captcha_register_ext():
    if request.method == 'OPTIONS':
        from flask import Response
        return Response('', 204, {'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Methods': 'POST', 'Access-Control-Allow-Headers': 'Content-Type'})
    from core.captcha_server import _registered, _lock
    import time
    data = request.get_json(silent=True) or {}
    account = data.get("account") or "unknown"
    with _lock:
        _registered[account] = time.time()
    return jsonify({"ok": True, "account": account})


@app.route("/captcha/heartbeat", methods=["POST"])
def captcha_heartbeat_ext():
    from core.captcha_server import _registered, _lock
    import time
    data = request.get_json(silent=True) or {}
    account = data.get("account") or "unknown"
    with _lock:
        _registered[account] = time.time()
    return jsonify({"ok": True})


@app.route("/captcha/poll", methods=["GET"])
def captcha_poll_ext():
    from core.captcha_server import _registered, _pending_requests, _lock, POLL_TIMEOUT, _worker_activity
    import time
    account = request.args.get("account")
    if not account:
        return jsonify({"error": "account required"}), 400

    with _lock:
        _registered[account] = time.time()
        _worker_activity[account] = {"phase": "IDLE", "request_id": "", "since": time.time()}

    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        with _lock:
            for req_id, req in _pending_requests.items():
                if req.get("account") is None and not req.get("sent"):
                    req["account"] = account
                    req["sent"] = True
                    _worker_activity[account] = {
                        "phase": "SOLVING",
                        "request_id": req_id,
                        "action": req["action"],
                        "since": time.time(),
                    }
                    return jsonify({"requestId": req_id, "action": req["action"]})
        time.sleep(0.4)

    return "", 204  # No Content — không có gì, extension sẽ poll lại


@app.route("/captcha/result", methods=["POST"])
def captcha_result_ext():
    from core.captcha_server import _pending_requests, _lock, _worker_activity
    data = request.get_json(silent=True) or {}
    request_id = data.get("requestId")
    token = data.get("token")
    account = data.get("account", "?")

    import time as _t
    with _lock:
        pending = _pending_requests.get(request_id)
        if account in _worker_activity:
            _worker_activity[account] = {"phase": "IDLE", "request_id": "", "since": _t.time()}

    if pending:
        pending["token"] = token
        pending["event"].set()
        if token:
            pass
        else:
            pass
        return jsonify({"ok": True})
    return jsonify({"error": "unknown requestId"}), 404


@app.route("/captcha/unregister", methods=["POST"])
def captcha_unregister_ext():
    from core.captcha_server import _registered, _lock
    data = request.get_json(silent=True) or {}
    account = data.get("account")
    if account:
        with _lock:
            _registered.pop(account, None)
    return jsonify({"ok": True})


@app.route("/api/browser-headers", methods=["POST"])
def receive_browser_headers_ext():
    data = request.get_json(silent=True) or {}
    updated = []

    try:
        from core import browser_config as bcfg_mod
        for key in ("x_client_data", "x_browser_validation", "x_browser_channel"):
            val = data.get(key)
            if val:
                bcfg_mod.set_value(key, val)
                updated.append(key)

        if updated:
            bcfg_mod.reload()
    except Exception as e:
        pass

    return jsonify({"ok": True, "updated": updated})


_GALLERY_DIR = os.path.join(ROOT_DIR, "image")
_GALLERY_ALLOWED_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def _gallery_ensure_dir():
    os.makedirs(_GALLERY_DIR, exist_ok=True)


def _gallery_safe_name(name: str) -> str:
    from werkzeug.utils import secure_filename
    return secure_filename(name or "") or ""


@app.route("/api/gallery/upload", methods=["POST"])
def gallery_upload():
    _gallery_ensure_dir()
    files = []
    for key in ("file", "files", "files[]"):
        files.extend(request.files.getlist(key))
    if not files:
        return jsonify({"error": "Không có file (dùng field 'file' hoặc 'files[]')"}), 400

    uploaded = []
    failed = []
    for f in files:
        if not f or not f.filename:
            continue
        safe = _gallery_safe_name(f.filename)
        if not safe:
            failed.append({"name": f.filename, "reason": "tên file không hợp lệ"})
            continue
        ext = os.path.splitext(safe)[1].lower()
        if ext not in _GALLERY_ALLOWED_EXTS:
            failed.append({"name": f.filename, "reason": f"ext {ext} không được phép"})
            continue
        dest = os.path.join(_GALLERY_DIR, safe)
        if os.path.exists(dest):
            base, e = os.path.splitext(safe)
            safe = f"{base}_{int(time.time())}{e}"
            dest = os.path.join(_GALLERY_DIR, safe)
        try:
            f.save(dest)
            uploaded.append({
                "name": safe,
                "size": os.path.getsize(dest),
                "url": f"/api/gallery/file/{safe}",
            })
        except Exception as e:
            failed.append({"name": f.filename, "reason": str(e)[:200]})

    return jsonify({
        "ok": True,
        "count": len(uploaded),
        "uploaded": uploaded,
        "failed": failed,
    })


@app.route("/api/gallery/list", methods=["GET"])
def gallery_list():
    _gallery_ensure_dir()
    items = []
    try:
        for name in sorted(os.listdir(_GALLERY_DIR)):
            fp = os.path.join(_GALLERY_DIR, name)
            if not os.path.isfile(fp):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in _GALLERY_ALLOWED_EXTS:
                continue
            try:
                st = os.stat(fp)
                items.append({
                    "name": name,
                    "size": st.st_size,
                    "modified_at": int(st.st_mtime),
                    "url": f"/api/gallery/file/{name}",
                })
            except Exception:
                continue
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500
    return jsonify({"ok": True, "count": len(items), "items": items})


@app.route("/api/gallery/file/<path:filename>", methods=["GET"])
def gallery_download(filename):
    _gallery_ensure_dir()
    safe = _gallery_safe_name(filename)
    if not safe:
        return jsonify({"error": "Tên file không hợp lệ"}), 400
    fp = os.path.join(_GALLERY_DIR, safe)
    if not os.path.isfile(fp):
        return jsonify({"error": "Không tìm thấy ảnh"}), 404
    force_download = str(request.args.get("download", "")).strip().lower() in ("1", "true", "yes")
    return send_file(fp, as_attachment=force_download, download_name=safe)


@app.route("/api/gallery/<path:filename>", methods=["DELETE"])
def gallery_delete(filename):
    _gallery_ensure_dir()
    safe = _gallery_safe_name(filename)
    if not safe:
        return jsonify({"error": "Tên file không hợp lệ"}), 400
    fp = os.path.join(_GALLERY_DIR, safe)
    if not os.path.isfile(fp):
        return jsonify({"error": "Không tìm thấy ảnh"}), 404
    try:
        os.remove(fp)
    except Exception as e:
        return jsonify({"error": f"Xoá thất bại: {e}"}), 500
    return jsonify({"ok": True, "deleted": safe})


_BANANA_INGEST_KEY = "admin1103"  # query param key để authenticate
_BANANA_TOKEN_FILE = os.path.join(ROOT_DIR, "services", "banana_token.json")


@app.route("/api/banana/token-ingest", methods=["POST"])
def banana_token_ingest():
    import json as _json
    if request.args.get("key") != _BANANA_INGEST_KEY:
        return jsonify({"success": False, "error": "Invalid key"}), 401
    try:
        body = request.get_json(silent=True) or {}
        token = (body.get("token") or "").strip()
        cookie = (body.get("cookie") or "").strip()
        if not token or not cookie:
            return jsonify({"success": False, "error": "Missing token or cookie"}), 400
        os.makedirs(os.path.dirname(_BANANA_TOKEN_FILE), exist_ok=True)
        with open(_BANANA_TOKEN_FILE, "w", encoding="utf-8") as f:
            _json.dump(
                {"token": token, "cookie": cookie, "updated_at": int(time.time())},
                f,
                ensure_ascii=False,
            )
        return jsonify({"success": True, "saved_at": int(time.time())})
    except Exception as e:
        logger.exception(f"[Banana] Ingest error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/banana/token-current", methods=["GET"])
def banana_token_current():
    import json as _json
    if request.args.get("key") != _BANANA_INGEST_KEY:
        return jsonify({"success": False, "error": "Invalid key"}), 401
    try:
        if not os.path.exists(_BANANA_TOKEN_FILE):
            return jsonify({"success": False, "error": "No token ingested yet"}), 404
        with open(_BANANA_TOKEN_FILE, "r", encoding="utf-8") as f:
            data = _json.load(f)
        return jsonify({"success": True, **data})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


try:
    from core import browser_task_server as _btask_srv
    _btask_srv.register_routes(app)
except Exception as _e:
    pass


def create_app():
    return app


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="VietAuto-style REST API Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, help="Port (default: 8080)")
    parser.add_argument("--debug", action="store_true", help="Debug mode")
    parser.add_argument("--api-key", default="", help="Set master API key")
    args = parser.parse_args()

    if args.api_key:
        os.environ["VIETAUTO_API_KEY"] = args.api_key


    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
