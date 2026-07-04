import os
import sys
import json
import time
import threading
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

_DIR = os.path.dirname(os.path.abspath(__file__))
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

from core import config as cfg
from core.cookie_manager import (
    load_cookies,
    save_cookies,
    extract_session_token_cookie,
    DEFAULT_COOKIES_PATH,
)
from core.veo_client import VeoClient
from core.imagen_client import ImagenClient
from core.project import create_project

job_queue = []
job_lock = threading.Lock()
runner_thread = None
running = False
cookie_cache = None
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"

MACHINE_STORE_PATH = os.path.join(_DIR, "data", "machines.json")
MACHINE_JOB_STORE_PATH = os.path.join(_DIR, "data", "machine_jobs.json")
MACHINE_ONLINE_TTL_SECONDS = 60
machine_lock = threading.Lock()
machine_job_lock = threading.Lock()


def now_ts():
    return int(time.time())


def load_machines():
    if not os.path.exists(MACHINE_STORE_PATH):
        return {}
    try:
        with open(MACHINE_STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_machines(machines):
    os.makedirs(os.path.dirname(MACHINE_STORE_PATH), exist_ok=True)
    tmp_path = MACHINE_STORE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(machines, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, MACHINE_STORE_PATH)


def is_machine_online(machine, ts=None):
    ts = ts or now_ts()
    last_seen = int(machine.get("last_heartbeat_at") or 0)
    return last_seen > 0 and (ts - last_seen) <= MACHINE_ONLINE_TTL_SECONDS


def machine_public_view(machine, ts=None):
    ts = ts or now_ts()
    view = dict(machine)
    view.pop("machine_secret", None)
    computed_status = "online" if is_machine_online(machine, ts) else "offline"
    view["computed_status"] = computed_status
    view["is_online"] = computed_status == "online"
    last_seen = int(machine.get("last_heartbeat_at") or 0)
    view["heartbeat_age_seconds"] = ts - last_seen if last_seen else None
    return view


def load_machine_jobs():
    if not os.path.exists(MACHINE_JOB_STORE_PATH):
        return []
    try:
        with open(MACHINE_JOB_STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_machine_jobs(jobs):
    os.makedirs(os.path.dirname(MACHINE_JOB_STORE_PATH), exist_ok=True)
    tmp_path = MACHINE_JOB_STORE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, MACHINE_JOB_STORE_PATH)


def require_machine_auth(data):
    machine_id = get_required_str(data, "machine_id")
    machine_secret = get_required_str(data, "machine_secret")
    if not machine_id or not machine_secret:
        return None, None, "machine_id and machine_secret are required"
    with machine_lock:
        machine = load_machines().get(machine_id)
    if not machine:
        return None, None, "Machine not registered"
    if str(machine.get("machine_secret") or "") != machine_secret:
        return None, None, "Invalid machine_secret"
    return machine_id, machine, None


def supported_action_types(supported_modes):
    modes = {str(x).strip().lower() for x in (supported_modes or []) if str(x).strip()}
    if not modes:
        modes = {"image", "video"}
    action_types = []
    if "video" in modes:
        action_types.extend(["TEXT_TO_VIDEO", "IMAGE_TO_VIDEO", "IMAGES_TO_VIDEO", "FRAMES_TO_VIDEO", "REFERENCE_IMAGE_TO_VIDEO"])
    if "image" in modes:
        action_types.extend(["CREATE_IMAGE", "IMAGES_TO_IMAGE"])
    return set(action_types)


def machine_job_contract(job):
    raw = job.get("raw_result") if isinstance(job.get("raw_result"), dict) else {}
    worker_claim = raw.get("worker_claim") if isinstance(raw.get("worker_claim"), dict) else {}
    image_paths = raw.get("image_paths") or job.get("image_paths") or []
    if not isinstance(image_paths, list):
        image_paths = [image_paths]
    return {
        "job_id": job.get("job_id") or job.get("id"),
        "action_type": job.get("action_type"),
        "prompts": job.get("prompts") or [],
        "model": job.get("model"),
        "screen_ratio": job.get("screen_ratio"),
        "upsample_resolution": raw.get("upsample_resolution") or job.get("upsample_resolution"),
        "image_paths": image_paths,
        "attempts": int(worker_claim.get("attempts") or job.get("attempts") or 0),
    }


def get_required_str(data, key):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def get_cookie():
    global cookie_cache
    if cookie_cache:
        return cookie_cache
    cookies = load_cookies()
    if not cookies:
        return None
    c = extract_session_token_cookie(cookies)
    cookie_cache = c
    return c


def process_job(job):
    cookie = get_cookie()
    if not cookie:
        job["status"] = STATUS_ERROR
        job["error"] = "No cookie"
        return

    mode = job.get("mode", "t2v")
    prompt = job.get("prompt", "")
    aspect = job.get("aspect", "VIDEO_ASPECT_RATIO_9_16")
    count = int(job.get("count", 1))

    try:
        if mode == "t2i":
            from core.banana_runtime.scheduler import BananaJob, BananaScheduler
            client = ImagenClient(cookie)
            veo = VeoClient(cookie)
            project_id = create_project(
                "AutoVoice Web", cookie=cookie, tool_name="PINHOLE"
            )
            if not project_id:
                job["status"] = STATUS_ERROR
                job["error"] = "Project creation failed"
                return
            access_token = getattr(veo, "access_token", None)
            if not access_token:
                job["status"] = STATUS_ERROR
                job["error"] = "No access token for shared image runtime"
                return
            scheduler = BananaScheduler(tokens=[access_token], thread_count=max(1, count), max_attempts=5)
            output_path = os.path.join(_DIR, "outputs", f"server_image_{job['id']}.png")
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            result = scheduler.submit([
                BananaJob(
                    prompt=prompt,
                    model="NARWHAL",
                    aspect_ratio={
                        "IMAGE_ASPECT_RATIO_LANDSCAPE": "16:9",
                        "IMAGE_ASPECT_RATIO_SQUARE": "1:1",
                    }.get(aspect, "9:16"),
                    output_path=output_path,
                    job_id=f"server-image-{job['id']}",
                )
            ])
        else:
            client = VeoClient(cookie)
            project_id = create_project("AutoVoice Web", cookie=cookie)
            if not project_id:
                job["status"] = STATUS_ERROR
                job["error"] = "Project creation failed"
                return

            if mode == "i2v":
                token = "BROWSER_RUNTIME_RECAPTCHA_IN_PAGE"

                result = client.create_video_i2v(
                    row=job["id"],
                    prompt=prompt,
                    project_id=project_id,
                    captcha_token=token,
                    start_image_media_id=job.get("start_image_id", ""),
                    end_image_media_id=job.get("end_image_id"),
                    aspect=aspect,
                    count=count,
                )
            else:
                token = "BROWSER_RUNTIME_RECAPTCHA_IN_PAGE"

                result = client.create_video_t2v(
                    row=job["id"],
                    prompt=prompt,
                    project_id=project_id,
                    captcha_token=token,
                    aspect=aspect,
                    count=count,
                )

        if result:
            job["status"] = STATUS_DONE
            job["result"] = result
        else:
            job["status"] = STATUS_ERROR
            job["error"] = "API returned no result"
    except Exception as e:
        job["status"] = STATUS_ERROR
        job["error"] = str(e)


def runner_loop(job_ids):
    global running
    running = True
    with job_lock:
        todos = [
            j for j in job_queue if j["id"] in job_ids and j["status"] == STATUS_PENDING
        ]
    for job in todos:
        if not running:
            break
        with job_lock:
            job["status"] = STATUS_RUNNING
        process_job(job)
    running = False


def cors(handler):
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")


def json_response(handler, data, code=200):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    cors(handler)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", len(body))
    handler.end_headers()
    handler.wfile.write(body)


def html_response(handler, html, code=200):
    body = html.encode("utf-8")
    handler.send_response(code)
    cors(handler)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", len(body))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_OPTIONS(self):
        self.send_response(204)
        cors(self)
        self.end_headers()

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/" or p == "/index.html":
            html_path = os.path.join(_DIR, "ui", "index.html")
            if os.path.exists(html_path):
                with open(html_path, "r", encoding="utf-8") as f:
                    html_response(self, f.read())
            else:
                html_response(self, "<h1>UI not found</h1>", 404)
        elif p == "/api/status":
            cookie = get_cookie()
            json_response(
                self,
                {
                    "connected": bool(cookie),
                    "jobs": job_queue,
                    "running": running,
                },
            )
        elif p == "/api/cookie":
            cookie = get_cookie()
            json_response(
                self,
                {
                    "connected": bool(cookie),
                    "preview": cookie[:40] + "..." if cookie else None,
                },
            )
        elif p == "/api/machines":
            ts = now_ts()
            with machine_lock:
                machines = load_machines()
                items = [machine_public_view(m, ts) for m in machines.values()]
            items.sort(key=lambda m: m.get("last_heartbeat_at") or 0, reverse=True)
            summary = {
                "total": len(items),
                "online": sum(1 for m in items if m.get("is_online")),
                "accepting": sum(
                    1
                    for m in items
                    if m.get("is_online") and bool(m.get("accepting_jobs"))
                ),
                "available_slots": sum(
                    int(m.get("available_slots") or 0)
                    for m in items
                    if m.get("is_online")
                ),
                "running_jobs": sum(
                    int(m.get("running_jobs") or 0)
                    for m in items
                    if m.get("is_online")
                ),
            }
            json_response(self, {"ok": True, "machines": items, "summary": summary})
        elif p == "/api/machines/check" or p.startswith("/api/machines/"):
            if p == "/api/machines/check":
                machine_id = (parse_qs(urlparse(self.path).query).get("machine_id") or [""])[0].strip()
            else:
                machine_id = p.rsplit("/", 1)[-1].strip()
            if not machine_id:
                json_response(self, {"ok": False, "error": "machine_id is required"}, 400)
                return
            ts = now_ts()
            with machine_lock:
                machines = load_machines()
                machine = machines.get(machine_id)
            json_response(
                self,
                {
                    "ok": True,
                    "machine_id": machine_id,
                    "registered": bool(machine),
                    "machine": machine_public_view(machine, ts) if machine else None,
                },
            )
        else:
            json_response(self, {"error": "Not found"}, 404)

    def do_POST(self):
        global running, runner_thread, cookie_cache
        p = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(body)
        except Exception:
            data = {}

        if p == "/api/cookie/import":
            cookies = data.get("cookies")
            if not cookies:
                json_response(self, {"ok": False, "error": "No cookies provided"})
                return
            if isinstance(cookies, str):
                try:
                    cookies = json.loads(cookies)
                except Exception:
                    pass
            save_cookies(cookies)
            cookie_cache = None
            json_response(self, {"ok": True})

        elif p == "/api/jobs/add":
            prompts = data.get("prompts", [])
            mode = data.get("mode", "t2v")
            aspect = data.get("aspect", "VIDEO_ASPECT_RATIO_9_16")
            count = int(data.get("count", 1))
            with job_lock:
                for prompt in prompts:
                    if not prompt.strip():
                        continue
                    job_id = int(time.time() * 1000) + len(job_queue)
                    job_queue.append(
                        {
                            "id": job_id,
                            "prompt": prompt.strip(),
                            "mode": mode,
                            "aspect": aspect,
                            "count": count,
                            "status": STATUS_PENDING,
                            "result": None,
                            "error": None,
                        }
                    )
            json_response(self, {"ok": True, "total": len(job_queue)})

        elif p == "/api/jobs/run":
            ids = data.get("ids")
            with job_lock:
                if ids is None:
                    run_ids = [
                        j["id"] for j in job_queue if j["status"] == STATUS_PENDING
                    ]
                else:
                    run_ids = ids
            if not running:
                runner_thread = threading.Thread(
                    target=runner_loop, args=(run_ids,), daemon=True
                )
                runner_thread.start()
            json_response(self, {"ok": True, "queued": len(run_ids)})

        elif p == "/api/jobs/stop":
            running = False
            json_response(self, {"ok": True})

        elif p == "/api/jobs/delete":
            ids = data.get("ids", [])
            with job_lock:
                for jid in ids:
                    for j in job_queue:
                        if j["id"] == jid:
                            job_queue.remove(j)
                            break
            json_response(self, {"ok": True})

        elif p == "/api/jobs/clear":
            with job_lock:
                job_queue.clear()
            json_response(self, {"ok": True})

        elif p == "/api/machines/check":
            machine_id = get_required_str(data, "machine_id")
            if not machine_id:
                json_response(self, {"ok": False, "error": "machine_id is required"}, 400)
                return
            ts = now_ts()
            with machine_lock:
                machines = load_machines()
                machine = machines.get(machine_id)
            json_response(
                self,
                {
                    "ok": True,
                    "machine_id": machine_id,
                    "registered": bool(machine),
                    "machine": machine_public_view(machine, ts) if machine else None,
                },
            )

        elif p == "/api/machines/delete":
            machine_id = get_required_str(data, "machine_id")
            if not machine_id:
                json_response(self, {"ok": False, "error": "machine_id is required"}, 400)
                return
            with machine_lock:
                machines = load_machines()
                removed = machines.pop(machine_id, None)
                if not removed:
                    json_response(self, {"ok": False, "error": "Machine not registered"}, 404)
                    return
                save_machines(machines)
            json_response(
                self,
                {
                    "ok": True,
                    "deleted": True,
                    "machine_id": machine_id,
                    "machine": machine_public_view(removed, now_ts()),
                },
            )

        elif p == "/api/machines/register":
            machine_id = get_required_str(data, "machine_id")
            machine_secret = get_required_str(data, "machine_secret")
            public_url = get_required_str(data, "public_url")
            version = get_required_str(data, "version") or "unknown"
            if not machine_id or not machine_secret or not public_url:
                json_response(
                    self,
                    {
                        "ok": False,
                        "error": "machine_id, machine_secret and public_url are required",
                    },
                    400,
                )
                return

            ts = now_ts()
            with machine_lock:
                machines = load_machines()
                existing = machines.get(machine_id, {})
                machine = {
                    **existing,
                    "machine_id": machine_id,
                    "machine_secret": machine_secret,
                    "public_url": public_url,
                    "version": version,
                    "status": existing.get("status", "registered"),
                    "token_count": int(existing.get("token_count") or 0),
                    "available_slots": int(existing.get("available_slots") or 0),
                    "running_jobs": int(existing.get("running_jobs") or 0),
                    "accepting_jobs": bool(existing.get("accepting_jobs", False)),
                    "frp_status": existing.get("frp_status", "unknown"),
                    "registered_at": existing.get("registered_at") or ts,
                    "updated_at": ts,
                    "last_heartbeat_at": existing.get("last_heartbeat_at") or None,
                }
                machines[machine_id] = machine
                save_machines(machines)
            json_response(
                self,
                {"ok": True, "machine": machine_public_view(machine, ts)},
            )

        elif p == "/api/machines/heartbeat":
            machine_id = get_required_str(data, "machine_id")
            if not machine_id:
                json_response(self, {"ok": False, "error": "machine_id is required"}, 400)
                return

            ts = now_ts()
            with machine_lock:
                machines = load_machines()
                machine = machines.get(machine_id)
                if not machine:
                    json_response(
                        self,
                        {
                            "ok": False,
                            "error": "Machine not registered. Call /api/machines/register first.",
                        },
                        404,
                    )
                    return

                machine.update(
                    {
                        "public_url": get_required_str(data, "public_url")
                        or machine.get("public_url"),
                        "status": get_required_str(data, "status") or "online",
                        "api_key_hash": str(data.get("api_key_hash") or machine.get("api_key_hash") or "").strip(),
                        "supported_modes": data.get("supported_modes") if isinstance(data.get("supported_modes"), list) else machine.get("supported_modes", ["image", "video"]),
                        "token_count": int(data.get("token_count") or 0),
                        "max_concurrent_jobs": int(data.get("max_concurrent_jobs") or machine.get("max_concurrent_jobs") or 0),
                        "available_slots": int(data.get("available_slots") or 0),
                        "running_jobs": int(data.get("running_jobs") or 0),
                        "accepting_jobs": bool(data.get("accepting_jobs", False)),
                        "frp_status": get_required_str(data, "frp_status") or "unknown",
                        "recent_error_count": int(data.get("recent_error_count") or machine.get("recent_error_count") or 0),
                        "cooldown_until": data.get("cooldown_until", machine.get("cooldown_until")),
                        "avg_duration_seconds": data.get("avg_duration_seconds", machine.get("avg_duration_seconds")),
                        "last_error": str(data.get("last_error") or machine.get("last_error") or "").strip(),
                        "updated_at": ts,
                        "last_heartbeat_at": ts,
                    }
                )
                machines[machine_id] = machine
                save_machines(machines)
            json_response(
                self,
                {"ok": True, "machine": machine_public_view(machine, ts)},
            )

        elif p == "/api/machines/jobs/claim":
            machine_id, machine, auth_error = require_machine_auth(data)
            if auth_error:
                json_response(self, {"success": False, "error": auth_error}, 401 if auth_error == "Invalid machine_secret" else 404)
                return
            available_slots = max(0, int(data.get("available_slots") or machine.get("available_slots") or 0))
            running_jobs = max(0, int(data.get("running_jobs") or machine.get("running_jobs") or 0))
            supported_modes = data.get("supported_modes") if isinstance(data.get("supported_modes"), list) else []
            allowed_actions = supported_action_types(supported_modes)
            ts = now_ts()

            with machine_lock:
                machines = load_machines()
                machine = machines.get(machine_id, machine)
                machine.update({
                    "available_slots": available_slots,
                    "running_jobs": running_jobs,
                    "accepting_jobs": bool(data.get("accepting_jobs", machine.get("accepting_jobs", True))),
                    "supported_modes": supported_modes or ["image", "video"],
                    "status": "online",
                    "updated_at": ts,
                    "last_heartbeat_at": ts,
                })
                machines[machine_id] = machine
                save_machines(machines)

            claimed = []
            if available_slots > 0 and machine.get("accepting_jobs", True):
                with machine_job_lock:
                    jobs = load_machine_jobs()
                    for job in jobs:
                        if len(claimed) >= available_slots:
                            break
                        if str(job.get("status") or "").upper() != "PENDING":
                            continue
                        if allowed_actions and job.get("action_type") not in allowed_actions:
                            continue
                        raw = job.get("raw_result") if isinstance(job.get("raw_result"), dict) else {}
                        worker_claim = dict(raw.get("worker_claim") or {})
                        worker_claim.update({
                            "assigned_machine_id": machine_id,
                            "assigned_at": ts,
                            "attempts": int(worker_claim.get("attempts") or 0) + 1,
                        })
                        raw["worker_claim"] = worker_claim
                        job["raw_result"] = raw
                        job["status"] = "PROCESSING"
                        job["updated_at"] = ts
                        claimed.append(machine_job_contract(job))
                    save_machine_jobs(jobs)
            json_response(self, {"success": True, "data": {"jobs": claimed}})

        elif p.startswith("/api/machines/jobs/") and p.endswith("/result"):
            job_id = p.split("/api/machines/jobs/", 1)[-1].rsplit("/result", 1)[0].strip()
            machine_id, _machine, auth_error = require_machine_auth(data)
            if auth_error:
                json_response(self, {"success": False, "error": auth_error}, 401 if auth_error == "Invalid machine_secret" else 404)
                return
            status = str(data.get("status") or "").strip().lower()
            if status not in ("completed", "failed"):
                json_response(self, {"success": False, "error": "status must be completed or failed"}, 400)
                return
            with machine_job_lock:
                jobs = load_machine_jobs()
                target = None
                for job in jobs:
                    if str(job.get("job_id") or job.get("id")) == job_id:
                        target = job
                        break
                if not target:
                    json_response(self, {"success": False, "error": "Job not found"}, 404)
                    return
                raw = target.get("raw_result") if isinstance(target.get("raw_result"), dict) else {}
                worker_claim = raw.get("worker_claim") if isinstance(raw.get("worker_claim"), dict) else {}
                if worker_claim.get("assigned_machine_id") and worker_claim.get("assigned_machine_id") != machine_id:
                    json_response(self, {"success": False, "error": "Job not assigned to this machine"}, 403)
                    return
                raw["worker_result"] = {
                    "machine_id": machine_id,
                    "status": status,
                    "download_url": data.get("download_url"),
                    "duration_seconds": data.get("duration_seconds"),
                    "attempts": data.get("attempts"),
                    "error": data.get("error"),
                    "reported_at": now_ts(),
                }
                if data.get("download_url"):
                    raw["download_url"] = data.get("download_url")
                    target["media_id"] = data.get("download_url")
                target["raw_result"] = raw
                target["status"] = "COMPLETED" if status == "completed" else "FAILED"
                target["error"] = data.get("error") if status == "failed" else None
                target["completed_at"] = now_ts()
                target["updated_at"] = now_ts()
                save_machine_jobs(jobs)
            json_response(self, {"success": True, "message": "Job result saved"})

        else:
            json_response(self, {"error": "Not found"}, 404)

    def do_DELETE(self):
        p = urlparse(self.path).path
        if p.startswith("/api/machines/"):
            machine_id = p.rsplit("/", 1)[-1].strip()
            if not machine_id:
                json_response(self, {"ok": False, "error": "machine_id is required"}, 400)
                return
            with machine_lock:
                machines = load_machines()
                removed = machines.pop(machine_id, None)
                if not removed:
                    json_response(self, {"ok": False, "error": "Machine not registered"}, 404)
                    return
                save_machines(machines)
            json_response(
                self,
                {
                    "ok": True,
                    "deleted": True,
                    "machine_id": machine_id,
                    "machine": machine_public_view(removed, now_ts()),
                },
            )
        else:
            json_response(self, {"error": "Not found"}, 404)


def main():
    port = 7860
    server = HTTPServer(("127.0.0.1", port), Handler)
    print(f"[AutoVoice] Server running at http://127.0.0.1:{port}")
    threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[AutoVoice] Server stopped.")


if __name__ == "__main__":
    main()
