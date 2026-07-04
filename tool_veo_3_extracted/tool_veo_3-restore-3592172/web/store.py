"""
MongoDB storage cho projects và tasks.
"""

import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, List
import pymongo
import certifi

from web.models import (
    Project,
    VideoTask,
    ImageTask,
    APIKey,
    User,
    UserRole,
    Permission,
    TaskStatus,
    VeoAccount,
    UserJobResult,
)

MONGO_URI = "mongodb+srv://phamvanlong11032000_db_user:VV4wI66EdtyozVux@cluster0.obbfrbk.mongodb.net/"
DB_NAME = "veo_db"
DATA_DIR = Path(__file__).resolve().parent / "data"
ROOT_DIR = Path(__file__).resolve().parent.parent
ASSIGNMENT_FILE = DATA_DIR / "veo_account_assignments.json"
JOB_ARCHIVE_DIR = ROOT_DIR / "job_result_archive"
LEGACY_JOB_ARCHIVE_DIR = DATA_DIR / "job_result_archive"
JOB_RESULT_MONGO_LIMIT_BYTES = 50 * 1024
JOB_ARCHIVE_TXT_MAX_BYTES = 100 * 1024 * 1024  # 100MB/file, then rotate to job_results_002.txt


class MongoStore:
    """Thread-safe MongoDB store cho projects, tasks, users."""

    def __init__(self):
        self._lock = threading.RLock()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        JOB_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

        # Connect to MongoDB with certifi (to avoid SSL errors on Windows)
        try:
            self.client = pymongo.MongoClient(MONGO_URI, tlsCAFile=certifi.where())
            self.db = self.client[DB_NAME]
            # Collections
            self._projects = self.db["projects"]
            self._video_tasks = self.db["video_tasks"]
            self._image_tasks = self.db["image_tasks"]
            self._users = self.db["users"]
            self._api_keys = self.db["api_keys"]
            self._veo_accounts = self.db["veo_accounts"]
            self._deleted_accounts = self.db["deleted_accounts"]
            self._settings = self.db["settings"]
            self._user_job_results = self.db["user_job_results"]

            # Khởi tạo Indexes
            self._users.create_index("username", unique=True)
            self._users.create_index("api_key", unique=True)
            self._projects.create_index("id", unique=True)
            self._video_tasks.create_index("id", unique=True)
            self._video_tasks.create_index([("project_id", 1), ("status", 1)])
            self._video_tasks.create_index([("project_id", 1), ("action_type", 1), ("status", 1)])
            self._video_tasks.create_index([("project_id", 1), ("created_at", -1)])
            self._video_tasks.create_index("created_at")
            self._veo_accounts.create_index("id", unique=True)
            self._deleted_accounts.create_index("name", unique=True)
            self._user_job_results.create_index([("user_id", 1), ("job_id", 1)], unique=True)
            self._user_job_results.create_index([("created_at", -1)])
            self._user_job_results.create_index([("status", 1), ("created_at", -1)])
            self._user_job_results.create_index([("project_name", 1), ("created_at", -1)])
            self._user_job_results.create_index([("user_id", 1), ("created_at", -1)])
            self._user_job_results.create_index([("user_id", 1), ("status", 1), ("created_at", -1)])
            self._user_job_results.create_index([("user_id", 1), ("project_name", 1), ("created_at", -1)])

            print("[MongoStore] Connected to MongoDB Atlas successfully.")
        except Exception as e:
            print(f"[MongoStore] Failed to connect DB: {e}")
            raise e

        self._ensure_default_admin()

    def _ensure_default_admin(self):
        """Khởi tạo tài khoản Admin mặc định nếu DB trống."""
        from werkzeug.security import generate_password_hash

        admin_key = "admin-secret-key"

        if not self.get_user_by_api_key(admin_key):
            import uuid

            admin_user = User(
                id=str(uuid.uuid4()),
                username="admin",
                api_key=admin_key,
                password_hash=generate_password_hash("admin"),
                role=UserRole.ADMIN,
                permissions=[p.value for p in Permission],
            )
            self.create_user(admin_user)
            print("[MongoStore] Created default admin account.")

    # ─── HELPER: Deserialization ───
    def _doc_to_obj(self, doc: dict, cls_model):
        if not doc:
            return None
        try:
            doc.pop("_id", None)  # Remove MongoDB specific _id field
            # Lọc bỏ các field không tồn tại trong dataclass để tránh lỗi khi
            # DB schema không đồng bộ với code (VPS cũ / migration chưa deploy)
            import dataclasses
            if dataclasses.is_dataclass(cls_model):
                known_fields = {f.name for f in dataclasses.fields(cls_model)}
                doc = {k: v for k, v in doc.items() if k in known_fields}
            # Sanitize media_id: DB corruption guard — nếu lưu nhầm thành list,
            # lấy phần tử đầu tiên hoặc None để tránh AttributeError .startswith()
            if "media_id" in doc and isinstance(doc["media_id"], list):
                doc["media_id"] = doc["media_id"][0] if doc["media_id"] else None
            return cls_model(**doc)
        except Exception as e:
            print(f"[MongoStore] _doc_to_obj failed for {cls_model.__name__}: {e}")
            return None
    def _read_assignment_map_unlocked(self) -> Dict[str, List[str]]:
        try:
            if not ASSIGNMENT_FILE.exists():
                return {}
            with ASSIGNMENT_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            result: Dict[str, List[str]] = {}
            for user_id, ids in data.items():
                if not user_id:
                    continue
                if isinstance(ids, list):
                    clean_ids = [str(x).strip() for x in ids if str(x).strip()]
                    result[str(user_id)] = list(dict.fromkeys(clean_ids))
            return result
        except Exception as e:
            print(f"[MongoStore] Failed to read assignment file: {e}")
            return {}

    def _write_assignment_map_unlocked(self, data: Dict[str, List[str]]) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        clean = {
            str(user_id): list(dict.fromkeys(str(x).strip() for x in ids if str(x).strip()))
            for user_id, ids in (data or {}).items()
            if user_id and ids
        }
        tmp = ASSIGNMENT_FILE.with_suffix(f".tmp.{uuid.uuid4().hex}")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(clean, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, ASSIGNMENT_FILE)

    def _assignment_user_for_account_unlocked(self, account_id: str) -> Optional[str]:
        if not account_id:
            return None
        assignments = self._read_assignment_map_unlocked()
        for user_id, ids in assignments.items():
            if account_id in ids:
                return user_id
        return None

    def _next_job_archive_file(self, archive_dir: Path, incoming_bytes: int = 0) -> Path:
        """Return a daily TXT archive path, rotating when the current file is too large."""
        base_file = archive_dir / "job_results.txt"
        if not base_file.exists() or base_file.stat().st_size + incoming_bytes <= JOB_ARCHIVE_TXT_MAX_BYTES:
            return base_file
        index = 2
        while True:
            candidate = archive_dir / f"job_results_{index:03d}.txt"
            if not candidate.exists() or candidate.stat().st_size + incoming_bytes <= JOB_ARCHIVE_TXT_MAX_BYTES:
                return candidate
            index += 1

    def _archive_large_job_result_unlocked(self, data: dict) -> dict:
        try:
            raw = json.dumps(data, ensure_ascii=False, default=str)
            if len(raw.encode("utf-8")) <= JOB_RESULT_MONGO_LIMIT_BYTES:
                return data
            day = datetime.fromtimestamp(data.get("created_at") or data.get("updated_at") or datetime.now().timestamp()).strftime("%Y-%m-%d")
            archive_dir = JOB_ARCHIVE_DIR / day
            archive_dir.mkdir(parents=True, exist_ok=True)
            # One append-only TXT file per day. Each line is a JSON object (JSON Lines)
            # so we can append safely without rewriting the whole archive file.
            archive_file = archive_dir / "job_results.txt"
            archive_record_id = uuid.uuid4().hex
            archive_entry = {
                "archive_record_id": archive_record_id,
                "archived_at": datetime.now().timestamp(),
                "job_id": data.get("job_id") or data.get("id"),
                "data": data,
            }
            archive_line = json.dumps(archive_entry, ensure_ascii=False, default=str) + "\n"
            archive_file = self._next_job_archive_file(archive_dir, len(archive_line.encode("utf-8")))
            with archive_file.open("a", encoding="utf-8") as f:
                f.write(archive_line)
            trimmed = dict(data)
            for key in ("download_url", "error", "prompt"):
                val = trimmed.get(key)
                if isinstance(val, str) and len(val) > 4096:
                    trimmed[key] = val[:4096] + "... [archived]"
            trimmed["archive_path"] = str(archive_file)
            trimmed["archive_record_id"] = archive_record_id
            trimmed["archive_source"] = "daily_txt_json_lines"
            trimmed["archived_heavy_payload"] = True
            trimmed["archived_size_bytes"] = len(raw.encode("utf-8"))
            return trimmed
        except Exception as e:
            print(f"[MongoStore] Failed to archive large job result: {e}")
            return data

    def migrate_legacy_job_archives_to_daily_txt(self, *, remove_legacy: bool = False) -> dict:
        """Gom các file archive JSON cũ về một file TXT chung theo ngày.

        Legacy layout:
            web/data/job_result_archive/YYYY-MM-DD/<job_id>.json

        New layout:
            job_result_archive/YYYY-MM-DD/job_results.txt

        File TXT dùng JSON Lines: mỗi dòng là một JSON object để append an toàn.
        """
        summary = {
            "legacy_dir": str(LEGACY_JOB_ARCHIVE_DIR),
            "target_dir": str(JOB_ARCHIVE_DIR),
            "days": 0,
            "files_migrated": 0,
            "files_skipped": 0,
            "errors": [],
        }
        if not LEGACY_JOB_ARCHIVE_DIR.exists():
            return summary

        for day_dir in sorted(p for p in LEGACY_JOB_ARCHIVE_DIR.iterdir() if p.is_dir()):
            day = day_dir.name
            target_day_dir = JOB_ARCHIVE_DIR / day
            target_day_dir.mkdir(parents=True, exist_ok=True)
            target_file = target_day_dir / "job_results.txt"
            day_touched = False

            for legacy_file in sorted(day_dir.glob("*.json")):
                try:
                    with legacy_file.open("r", encoding="utf-8") as f:
                        data = json.load(f)
                    archive_record_id = uuid.uuid4().hex
                    archive_entry = {
                        "archive_record_id": archive_record_id,
                        "archived_at": datetime.now().timestamp(),
                        "migrated_from": str(legacy_file),
                        "job_id": data.get("job_id") or data.get("id") or legacy_file.stem,
                        "data": data,
                    }
                    archive_line = json.dumps(archive_entry, ensure_ascii=False, default=str) + "\n"
                    target_file = self._next_job_archive_file(target_day_dir, len(archive_line.encode("utf-8")))
                    with target_file.open("a", encoding="utf-8") as f:
                        f.write(archive_line)
                    summary["files_migrated"] += 1
                    day_touched = True
                    if remove_legacy:
                        try:
                            legacy_file.unlink()
                        except Exception as unlink_exc:
                            summary["errors"].append({"file": str(legacy_file), "error": f"unlink: {unlink_exc}"})
                except Exception as exc:
                    summary["files_skipped"] += 1
                    summary["errors"].append({"file": str(legacy_file), "error": str(exc)})

            if day_touched:
                summary["days"] += 1
            if remove_legacy:
                try:
                    day_dir.rmdir()
                except OSError:
                    pass

        return summary


    # ─── PROJECTS ───

    def create_project(self, project: Project) -> Project:
        with self._lock:
            self._projects.insert_one(project.to_dict())
            return project

    def get_project(self, project_id: str) -> Optional[Project]:
        with self._lock:
            doc = self._projects.find_one({"id": project_id})
            return self._doc_to_obj(doc, Project)

    def list_projects(self):
        with self._lock:
            docs = self._projects.find()
            return [self._doc_to_obj(d, Project) for d in docs]

    def update_project(self, project: Project) -> Project:
        with self._lock:
            import time
            project.updated_at = time.time()
            self._projects.update_one({"id": project.id}, {"$set": project.to_dict()})
            return project

    def delete_project(self, project_id: str) -> bool:
        with self._lock:
            self._video_tasks.delete_many({"project_id": project_id})
            result = self._projects.delete_one({"id": project_id})
            return result.deleted_count > 0

    # ─── VIDEO TASKS ───

    def create_video_task(self, task: VideoTask) -> VideoTask:
        with self._lock:
            self._video_tasks.insert_one(task.to_dict())
            # Cập nhật video_count của project
            self._projects.update_one(
                {"id": task.project_id}, {"$inc": {"video_count": 1}}
            )
            return task

    def get_video_task(self, task_id: str) -> Optional[VideoTask]:
        with self._lock:
            doc = self._video_tasks.find_one({"id": task_id})
            return self._doc_to_obj(doc, VideoTask)

    def update_video_task(self, task: VideoTask) -> VideoTask:
        with self._lock:
            import time

            task.updated_at = time.time()
            # Tự gán completed_at khi task rời khỏi trạng thái đang xử lý
            # (bất kể COMPLETED, FAILED, PAUSED hay bất kỳ status nào có kết quả)
            if task.status not in ("PENDING", "PROCESSING") and not task.completed_at:
                task.completed_at = time.time()
            # Reset completed_at khi retry (task quay lại PENDING/PROCESSING)
            elif task.status in ("PENDING", "PROCESSING"):
                task.completed_at = None
            self._video_tasks.update_one({"id": task.id}, {"$set": task.to_dict()})
            return task

    def claim_machine_jobs(
        self,
        *,
        machine_id: str,
        available_slots: int,
        supported_action_types: List[str],
    ) -> List[VideoTask]:
        """Atomically claim queued tasks for a pull-based worker machine."""
        import time

        machine_id = str(machine_id or "").strip()
        if not machine_id or available_slots <= 0:
            return []
        action_types = [str(x).strip() for x in (supported_action_types or []) if str(x).strip()]
        now = time.time()
        claimed: List[VideoTask] = []

        with self._lock:
            query = {
                "$or": [
                    {"status": "PENDING"},
                    {
                        "status": "PROCESSING",
                        "raw_result.public_dispatch.assigned_machine_id": machine_id,
                        "raw_result.worker_claim.assigned_machine_id": {"$in": [None, ""]},
                    },
                ]
            }
            if action_types:
                query["action_type"] = {"$in": action_types}

            candidates = list(
                self._video_tasks.find(query).sort("created_at", 1).limit(int(available_slots))
            )
            for doc in candidates:
                task_id = doc.get("id")
                if not task_id:
                    continue
                raw_result = doc.get("raw_result") if isinstance(doc.get("raw_result"), dict) else {}
                worker_claim = dict(raw_result.get("worker_claim") or {})
                attempts = int(worker_claim.get("attempts") or 0)
                worker_claim.update({
                    "assigned_machine_id": machine_id,
                    "assigned_at": now,
                    "attempts": attempts + 1,
                })
                raw_result["worker_claim"] = worker_claim

                result = self._video_tasks.find_one_and_update(
                    {
                        "id": task_id,
                        "$or": [
                            {"status": "PENDING"},
                            {
                                "status": "PROCESSING",
                                "raw_result.public_dispatch.assigned_machine_id": machine_id,
                            },
                        ],
                    },
                    {"$set": {
                        "status": "PROCESSING",
                        "updated_at": now,
                        "raw_result": raw_result,
                    }},
                    return_document=pymongo.ReturnDocument.AFTER,
                )
                obj = self._doc_to_obj(result, VideoTask) if result else None
                if obj:
                    claimed.append(obj)
        return claimed

    def save_machine_job_result(
        self,
        *,
        job_id: str,
        machine_id: str,
        status: str,
        download_url: Optional[str] = None,
        duration_seconds: Optional[float] = None,
        attempts: Optional[int] = None,
        error: Optional[str] = None,
    ) -> Optional[VideoTask]:
        """Save completed/failed result reported by the machine that claimed the job."""
        import time

        job_id = str(job_id or "").strip()
        machine_id = str(machine_id or "").strip()
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in ("completed", "failed"):
            return None

        with self._lock:
            doc = self._video_tasks.find_one({"id": job_id})
            if not doc:
                return None
            raw_result = doc.get("raw_result") if isinstance(doc.get("raw_result"), dict) else {}
            worker_claim = raw_result.get("worker_claim") if isinstance(raw_result.get("worker_claim"), dict) else {}
            if worker_claim.get("assigned_machine_id") and worker_claim.get("assigned_machine_id") != machine_id:
                return None

            now = time.time()
            raw_result["worker_result"] = {
                "machine_id": machine_id,
                "status": normalized_status,
                "download_url": download_url,
                "duration_seconds": duration_seconds,
                "attempts": attempts,
                "error": error,
                "reported_at": now,
            }
            if download_url:
                raw_result["download_url"] = download_url

            update = {
                "status": "COMPLETED" if normalized_status == "completed" else "FAILED",
                "updated_at": now,
                "completed_at": now,
                "raw_result": raw_result,
                "error": error if normalized_status == "failed" else None,
            }
            if download_url:
                update["media_id"] = download_url
                update["output_filename"] = os.path.basename(str(download_url).split("?", 1)[0]) or None

            result = self._video_tasks.find_one_and_update(
                {"id": job_id},
                {"$set": update},
                return_document=pymongo.ReturnDocument.AFTER,
            )
            return self._doc_to_obj(result, VideoTask) if result else None

    def list_public_dispatch_candidates(self, *, limit: int = 25, action_types: Optional[List[str]] = None) -> List[VideoTask]:
        """Return queued tasks that are not already assigned to a public Banana worker."""
        with self._lock:
            query = {"status": {"$in": ["PENDING", "QUEUED", TaskStatus.PENDING]}}
            if action_types:
                query["action_type"] = {"$in": action_types}
            docs = self._video_tasks.find(query).sort("created_at", 1).limit(max(1, int(limit or 25)))
            tasks: List[VideoTask] = []
            for doc in docs:
                raw = doc.get("raw_result") if isinstance(doc.get("raw_result"), dict) else {}
                public_dispatch = raw.get("public_dispatch") if isinstance(raw.get("public_dispatch"), dict) else {}
                if public_dispatch.get("remote_task_id") or public_dispatch.get("assigned_machine_id"):
                    continue
                obj = self._doc_to_obj(doc, VideoTask)
                if obj:
                    tasks.append(obj)
            return tasks

    def mark_public_dispatching(self, *, task_id: str, machine_id: str, metadata: dict) -> Optional[VideoTask]:
        import time

        with self._lock:
            doc = self._video_tasks.find_one({"id": task_id})
            if not doc:
                return None
            raw = doc.get("raw_result") if isinstance(doc.get("raw_result"), dict) else {}
            public_dispatch = raw.get("public_dispatch") if isinstance(raw.get("public_dispatch"), dict) else {}
            public_dispatch.update(metadata or {})
            public_dispatch.update({
                "assigned_machine_id": machine_id,
                "dispatched_at": time.time(),
            })
            raw["public_dispatch"] = public_dispatch
            result = self._video_tasks.find_one_and_update(
                {"id": task_id, "status": {"$in": ["PENDING", "QUEUED", TaskStatus.PENDING]}},
                {"$set": {"status": "PROCESSING", "updated_at": time.time(), "raw_result": raw}},
                return_document=pymongo.ReturnDocument.AFTER,
            )
            return self._doc_to_obj(result, VideoTask) if result else None

    def list_public_processing_tasks(self, *, limit: int = 100) -> List[VideoTask]:
        with self._lock:
            docs = self._video_tasks.find({
                "status": "PROCESSING",
                "raw_result.public_dispatch.remote_task_id": {"$nin": [None, ""]},
            }).sort("updated_at", 1).limit(max(1, int(limit or 100)))
            return [obj for obj in (self._doc_to_obj(d, VideoTask) for d in docs) if obj is not None]

    def save_public_dispatch_poll_result(self, *, task_id: str, remote_status: str, result_payload: dict) -> Optional[VideoTask]:
        import time

        normalized = str(remote_status or "").strip().upper()
        result_payload = result_payload if isinstance(result_payload, dict) else {}
        with self._lock:
            doc = self._video_tasks.find_one({"id": task_id})
            if not doc:
                return None
            raw = doc.get("raw_result") if isinstance(doc.get("raw_result"), dict) else {}
            public_dispatch = raw.get("public_dispatch") if isinstance(raw.get("public_dispatch"), dict) else {}
            public_dispatch["last_poll_at"] = time.time()
            public_dispatch["last_remote_status"] = normalized
            public_dispatch["poll_attempts"] = int(public_dispatch.get("poll_attempts") or 0) + 1
            public_dispatch["result"] = result_payload
            raw["public_dispatch"] = public_dispatch

            update = {"updated_at": time.time(), "raw_result": raw}
            if normalized == "COMPLETED":
                media_id = result_payload.get("media_id") or result_payload.get("download_url")
                media_ids = result_payload.get("media_ids") if isinstance(result_payload.get("media_ids"), list) else []
                if not media_id and media_ids:
                    media_id = media_ids[-1]
                results = result_payload.get("results") if isinstance(result_payload.get("results"), list) else []
                if not media_id and results:
                    media_id = next((r.get("download_url") or r.get("saved_path") for r in results if isinstance(r, dict)), None)
                update.update({
                    "status": "COMPLETED",
                    "completed_at": time.time(),
                    "media_id": media_id,
                    "output_filename": result_payload.get("output_filename"),
                    "error": None,
                })
            elif normalized == "FAILED":
                update.update({
                    "status": "FAILED",
                    "completed_at": time.time(),
                    "error": result_payload.get("error") or "Remote Banana task failed",
                })

            result = self._video_tasks.find_one_and_update(
                {"id": task_id},
                {"$set": update},
                return_document=pymongo.ReturnDocument.AFTER,
            )
            return self._doc_to_obj(result, VideoTask) if result else None

    def get_all_project_stats(self) -> dict:
        """Trả về thống kê số lượng video theo status cho từng project."""
        with self._lock:
            try:
                pipeline = [
                    {"$group": {"_id": {"project_id": "$project_id", "status": "$status"}, "count": {"$sum": 1}}}
                ]
                stats = {}
                for row in self._video_tasks.aggregate(pipeline, allowDiskUse=True):
                    key = row.get("_id") or {}
                    pid = key.get("project_id")
                    st = key.get("status")
                    if not pid or not st:
                        continue
                    stats.setdefault(pid, {})[st] = int(row.get("count", 0) or 0)
                return stats
            except Exception:
                # Fallback cũ nếu môi trường Mongo không cho aggregation.
                stats = {}
                project_ids = self._video_tasks.distinct("project_id")
                _statuses = ["COMPLETED", "FAILED", "ERROR", "PENDING", "PROCESSING", "PAUSED"]
                for pid in project_ids:
                    if not pid:
                        continue
                    stats[pid] = {}
                    for st in _statuses:
                        cnt = self._video_tasks.count_documents({"project_id": pid, "status": st})
                        stats[pid][st] = cnt
                return stats

    def get_all_project_image_stats(self) -> dict:
        """Trả về số ảnh THỰC TẾ được sinh ra cho mỗi project."""
        with self._lock:
            try:
                pipeline = [
                    {"$match": {"status": "COMPLETED", "action_type": "CREATE_IMAGE"}},
                    {"$project": {
                        "project_id": 1,
                        "img_count": {
                            "$cond": [
                                {"$isArray": "$raw_result.image_paths"},
                                {"$size": "$raw_result.image_paths"},
                                1,
                            ]
                        },
                    }},
                    {"$group": {"_id": "$project_id", "total": {"$sum": "$img_count"}}},
                ]
                return {row.get("_id"): int(row.get("total", 0) or 0) for row in self._video_tasks.aggregate(pipeline, allowDiskUse=True) if row.get("_id")}
            except Exception:
                result = {}
                docs = self._video_tasks.find(
                    {"status": "COMPLETED", "action_type": "CREATE_IMAGE"},
                    {"project_id": 1, "raw_result.image_paths": 1}
                )
                for doc in docs:
                    pid = doc.get("project_id")
                    if not pid:
                        continue
                    raw = doc.get("raw_result") or {}
                    paths = raw.get("image_paths", [])
                    img_count = len(paths) if isinstance(paths, list) and paths else 1
                    result[pid] = result.get(pid, 0) + img_count
                return result

    def list_video_tasks(
        self,
        project_id: Optional[str] = None,
        search: Optional[str] = None,
        status: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
        media_type: Optional[str] = None,   # "IMAGE" | "VIDEO" | None = ALL
        date_from: Optional[float] = None,  # Unix timestamp — lọc created_at >= date_from
    ):
        with self._lock:
            query = {}
            if project_id:
                query["project_id"] = project_id
            if status:
                if status == "PENDING":
                    query["status"] = {"$in": ["PENDING", "PROCESSING"]}
                elif status == "FAILED":
                    query["status"] = {"$in": ["FAILED", "ERROR"]}
                else:
                    query["status"] = status
            if search:
                query["$or"] = [
                    {"name": {"$regex": search, "$options": "i"}},
                    {"prompts": {"$regex": search, "$options": "i"}},
                ]
            # ── Media type filter ──
            if media_type and media_type.upper() == "IMAGE":
                query["action_type"] = "CREATE_IMAGE"
            elif media_type and media_type.upper() == "VIDEO":
                query["action_type"] = {"$ne": "CREATE_IMAGE"}
            # ── Date filter ──
            if date_from:
                query["created_at"] = {"$gte": date_from}

            # Đếm tổng số lượng bản ghi thỏa mãn điều kiện
            total_count = self._video_tasks.count_documents(query)

            # Sắp xếp theo created_at giảm dần (mới nhất trước)
            docs = (
                self._video_tasks.find(query)
                .sort("created_at", -1)
                .skip(skip)
                .limit(limit)
            )
            # Filter out None (corrupted docs that _doc_to_obj could not deserialize)
            results = [obj for obj in (self._doc_to_obj(d, VideoTask) for d in docs) if obj is not None]
            return results, total_count

    def delete_video_task(self, task_id: str) -> bool:
        with self._lock:
            res = self._video_tasks.delete_one({"id": task_id})
            return res.deleted_count > 0

    # ─── IMAGE TASKS ───

    def create_image_task(self, task: ImageTask) -> ImageTask:
        with self._lock:
            self._image_tasks.insert_one(task.to_dict())
            return task

    def get_image_task(self, task_id: str) -> Optional[ImageTask]:
        with self._lock:
            doc = self._image_tasks.find_one({"id": task_id})
            return self._doc_to_obj(doc, ImageTask)

    def update_image_task(self, task: ImageTask) -> ImageTask:
        with self._lock:
            import time

            task.updated_at = time.time()
            self._image_tasks.update_one({"id": task.id}, {"$set": task.to_dict()})
            return task

    # ─── API KEYS (Hỗ trợ Legacy tool cũ) ───

    def add_api_key(self, api_key: APIKey):
        with self._lock:
            self._api_keys.insert_one(api_key.to_dict())

    def get_api_key(self, key: str) -> Optional[APIKey]:
        with self._lock:
            doc = self._api_keys.find_one({"key": key})
            return self._doc_to_obj(doc, APIKey)

    def validate_api_key(self, key: str) -> bool:
        doc = self.get_api_key(key)
        return doc is not None and doc.is_active

    # ─── USERS (RBAC) ───

    def create_user(self, user: User) -> User:
        with self._lock:
            self._users.insert_one(user.to_dict())
            return user

    def get_user(self, user_id: str) -> Optional[User]:
        with self._lock:
            doc = self._users.find_one({"id": user_id})
            return self._doc_to_obj(doc, User)

    def get_user_by_username(self, username: str) -> Optional[User]:
        with self._lock:
            doc = self._users.find_one({"username": username})
            return self._doc_to_obj(doc, User)

    def get_user_by_api_key(self, api_key: str) -> Optional[User]:
        with self._lock:
            doc = self._users.find_one({"api_key": api_key})
            return self._doc_to_obj(doc, User)

    def update_user(self, user: User) -> User:
        with self._lock:
            self._users.update_one({"id": user.id}, {"$set": user.to_dict()})
            return user

    def delete_user(self, user_id: str) -> bool:
        with self._lock:
            assignments = self._read_assignment_map_unlocked()
            account_ids = assignments.pop(user_id, [])
            if account_ids:
                self._write_assignment_map_unlocked(assignments)
                self._veo_accounts.update_many(
                    {"id": {"$in": account_ids}, "assigned_to_user_id": user_id},
                    {"$unset": {"assigned_to_user_id": ""}},
                )
            self._veo_accounts.update_many(
                {"assigned_to_user_id": user_id},
                {"$unset": {"assigned_to_user_id": ""}},
            )
            result = self._users.delete_one({"id": user_id})
            return result.deleted_count > 0

    def list_users(self):
        with self._lock:
            docs = self._users.find()
            return [self._doc_to_obj(d, User) for d in docs]

    # ─── USER JOB RESULTS ───

    def upsert_user_job_result(self, result: UserJobResult) -> tuple[UserJobResult, bool]:
        """Insert/update theo unique key (user_id, job_id). Trả về (record, created)."""
        with self._lock:
            import time

            existing = self._user_job_results.find_one({
                "user_id": result.user_id,
                "job_id": result.job_id,
            })
            now = time.time()
            data = result.to_dict()
            data["updated_at"] = now
            if existing:
                data["id"] = existing.get("id") or result.id
                data["created_at"] = existing.get("created_at") or result.created_at
            else:
                data["created_at"] = now

            data = self._archive_large_job_result_unlocked(data)
            if existing:
                self._user_job_results.update_one(
                    {"user_id": result.user_id, "job_id": result.job_id},
                    {"$set": data},
                    upsert=True,
                )
                return self._doc_to_obj(data, UserJobResult), False

            self._user_job_results.insert_one(data)
            return self._doc_to_obj(data, UserJobResult), True

    def list_user_job_results(
        self,
        user_id: Optional[str] = None,
        project_name: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
    ):
        """Liệt kê job results cho web/admin, sort mới nhất trước."""
        with self._lock:
            query = {}
            if user_id:
                query["user_id"] = user_id
            if project_name:
                query["project_name"] = project_name
            if status:
                query["status"] = status
            if search:
                user_ids = [
                    u.id for u in self.list_users()
                    if u and getattr(u, "username", None) and search.lower() in u.username.lower()
                ]
                search_or = [
                    {"job_id": {"$regex": search, "$options": "i"}},
                    {"prompt": {"$regex": search, "$options": "i"}},
                    {"project_name": {"$regex": search, "$options": "i"}},
                ]
                if user_ids:
                    search_or.append({"user_id": {"$in": user_ids}})
                query["$or"] = search_or

            total_count = self._user_job_results.count_documents(query)
            docs = (
                self._user_job_results.find(query)
                .allow_disk_use(True)
                .sort("created_at", -1)
                .skip(skip)
                .limit(limit)
            )
            results = [obj for obj in (self._doc_to_obj(d, UserJobResult) for d in docs) if obj is not None]
            return results, total_count


    # ─── VEO ACCOUNTS (Proxy Pooling) ───

    def create_veo_account(self, account: VeoAccount) -> VeoAccount:
        with self._lock:
            # Overwrite if exists based on name
            self._veo_accounts.update_one(
                {"name": account.name},
                {"$set": account.to_dict()},
                upsert=True
            )
            return account

    def get_veo_account(self, account_id: str) -> Optional[VeoAccount]:
        with self._lock:
            doc = self._veo_accounts.find_one({"id": account_id})
            return self._doc_to_obj(doc, VeoAccount)

    def get_veo_account_by_name(self, account_name: str) -> Optional[VeoAccount]:
        with self._lock:
            doc = self._veo_accounts.find_one({"name": account_name})
            return self._doc_to_obj(doc, VeoAccount)

    def update_veo_account(self, account: VeoAccount) -> VeoAccount:
        with self._lock:
            self._veo_accounts.update_one({"id": account.id}, {"$set": account.to_dict()})
            return account

    def delete_veo_account(self, account_id: str) -> bool:
        with self._lock:
            # Lấy tên account trước khi xoá để ghi vào blacklist
            doc = self._veo_accounts.find_one({"id": account_id})
            if doc and doc.get("name"):
                self._add_deleted_account_name(doc["name"])
            result = self._veo_accounts.delete_one({"id": account_id})
            return result.deleted_count > 0

    def bulk_delete_veo_accounts(self, account_ids: List[str]) -> int:
        """Xoá nhiều accounts cùng lúc bằng 1 query MongoDB."""
        if not account_ids:
            return 0
        with self._lock:
            # Lấy tên tất cả accounts cần xoá để ghi vào blacklist
            docs = self._veo_accounts.find({"id": {"$in": account_ids}}, {"name": 1})
            for doc in docs:
                if doc.get("name"):
                    self._add_deleted_account_name(doc["name"])
            result = self._veo_accounts.delete_many({"id": {"$in": account_ids}})
            return result.deleted_count

    # ─── DELETED ACCOUNTS BLACKLIST ───

    def _add_deleted_account_name(self, name: str):
        """Ghi nhớ tên account đã xoá → chặn extension tự tạo lại."""
        import time
        try:
            self._deleted_accounts.update_one(
                {"name": name},
                {"$set": {"name": name, "deleted_at": time.time()}},
                upsert=True,
            )
        except Exception:
            pass  # Ignore duplicate key or other errors

    def is_account_deleted(self, name: str) -> bool:
        """Kiểm tra tên account có nằm trong danh sách đã xoá không."""
        with self._lock:
            return self._deleted_accounts.find_one({"name": name}) is not None

    def restore_deleted_account(self, name: str):
        """Gỡ account khỏi blacklist (cho phép tạo lại)."""
        with self._lock:
            self._deleted_accounts.delete_one({"name": name})

    def list_deleted_accounts(self) -> List[str]:
        """Liệt kê tất cả tên accounts đã bị xoá."""
        with self._lock:
            docs = self._deleted_accounts.find({}, {"name": 1})
            return [d["name"] for d in docs if d.get("name")]

    def bulk_unban_veo_accounts(self, account_ids: List[str]) -> int:
        """Gỡ ban (set is_active=True) nhiều accounts cùng lúc bằng 1 query."""
        if not account_ids:
            return 0
        with self._lock:
            result = self._veo_accounts.update_many(
                {"id": {"$in": account_ids}},
                {"$set": {"is_active": True}}
            )
            return result.modified_count

    def list_veo_accounts(self) -> List[VeoAccount]:
        with self._lock:
            docs = self._veo_accounts.find()
            accounts = [self._doc_to_obj(d, VeoAccount) for d in docs]
            assignments = self._read_assignment_map_unlocked()
            owner_by_account = {
                account_id: user_id
                for user_id, ids in assignments.items()
                for account_id in ids
            }
            for account in accounts:
                if account and account.id in owner_by_account:
                    account.assigned_to_user_id = owner_by_account[account.id]
            return accounts

    def get_veo_assignment_map(self) -> Dict[str, List[str]]:
        with self._lock:
            return self._read_assignment_map_unlocked()

    def get_random_active_veo_account(self) -> Optional[VeoAccount]:
        """Lấy ngẫu nhiên 1 tài khoản Veo đang active trong Pool.
        Ưu tiên account CHƯA gán user, nếu không có thì lấy bất kỳ active."""
        with self._lock:
            # Ưu tiên chưa gán
            pipeline_free = [
                {"$match": {"is_active": True, "$or": [{"assigned_to_user_id": None}, {"assigned_to_user_id": {"$exists": False}}]}},
                {"$sample": {"size": 1}}
            ]
            docs = list(self._veo_accounts.aggregate(pipeline_free))
            if docs:
                return self._doc_to_obj(docs[0], VeoAccount)
            # Fallback: bất kỳ active nào
            pipeline_any = [
                {"$match": {"is_active": True}},
                {"$sample": {"size": 1}}
            ]
            docs = list(self._veo_accounts.aggregate(pipeline_any))
            if docs:
                return self._doc_to_obj(docs[0], VeoAccount)
            return None

    def get_assigned_veo_account_for_user(self, user_id: str) -> Optional[VeoAccount]:
        """Lấy account đã được gán cho user_id (nếu có)."""
        with self._lock:
            assignments = self._read_assignment_map_unlocked()
            ids = assignments.get(user_id) or []
            if ids:
                doc = self._veo_accounts.find_one({"id": {"$in": ids}, "is_active": True})
                return self._doc_to_obj(doc, VeoAccount) if doc else None
            doc = self._veo_accounts.find_one({"assigned_to_user_id": user_id, "is_active": True})
            return self._doc_to_obj(doc, VeoAccount) if doc else None

    def assign_free_cookie_to_user(self, user_id: str) -> Optional[VeoAccount]:
        """Gán 1 cookie RẢNH (chưa gán) cho user_id.
        Nếu đã có account gán cho user, trả về account đó luôn.
        Ngược lại, tìm 1 account free, gán và lưu DB."""
        with self._lock:
            assignments = self._read_assignment_map_unlocked()
            existing_ids = assignments.get(user_id) or []
            if existing_ids:
                existing = self._veo_accounts.find_one({"id": {"$in": existing_ids}, "is_active": True})
                if existing:
                    return self._doc_to_obj(existing, VeoAccount)

            # Backward compatibility: nếu DB cũ đã gán thì migrate nhẹ sang file.
            existing = self._veo_accounts.find_one({"assigned_to_user_id": user_id, "is_active": True})
            if existing:
                assignments[user_id] = [existing["id"]]
                self._write_assignment_map_unlocked(assignments)
                return self._doc_to_obj(existing, VeoAccount)

            free = self._veo_accounts.find_one({
                "is_active": True,
                "cookie": {"$nin": [None, ""]},
                "$or": [{"assigned_to_user_id": None}, {"assigned_to_user_id": {"$exists": False}}, {"assigned_to_user_id": ""}],
            })
            if not free:
                return None
            assignments[user_id] = [free["id"]]
            self._write_assignment_map_unlocked(assignments)
            free["assigned_to_user_id"] = user_id
            return self._doc_to_obj(free, VeoAccount)

    def get_or_assign_veo_account_for_user(self, user_id: str) -> Optional[VeoAccount]:
        """Trả account đã gán cho user, hoặc atomic gán 1 account active còn rảnh.

        Dùng cho POST /api/veo/account-session. Mục tiêu là user gọi lại bằng
        cùng API key luôn nhận cùng account, còn user khác không thể lấy trùng.
        """
        if not user_id:
            return None
        with self._lock:
            assignments = self._read_assignment_map_unlocked()
            existing_ids = assignments.get(user_id) or []
            if existing_ids:
                existing = self._veo_accounts.find_one({
                    "id": {"$in": existing_ids},
                    "is_active": True,
                    "cookie": {"$nin": [None, ""]},
                })
                if existing:
                    return self._doc_to_obj(existing, VeoAccount)

            existing = self._veo_accounts.find_one({
                "assigned_to_user_id": user_id,
                "is_active": True,
                "cookie": {"$nin": [None, ""]},
            })
            if existing:
                assignments[user_id] = [existing["id"]]
                self._write_assignment_map_unlocked(assignments)
                return self._doc_to_obj(existing, VeoAccount)

            assigned_ids = {aid for ids in assignments.values() for aid in ids}
            query = {
                "is_active": True,
                "cookie": {"$nin": [None, ""]},
                "id": {"$nin": list(assigned_ids)},
                "$or": [
                    {"assigned_to_user_id": None},
                    {"assigned_to_user_id": {"$exists": False}},
                    {"assigned_to_user_id": ""},
                ],
            }
            doc = self._veo_accounts.find_one(query)
            if not doc:
                return None
            assignments[user_id] = [doc["id"]]
            self._write_assignment_map_unlocked(assignments)
            doc["assigned_to_user_id"] = user_id
            return self._doc_to_obj(doc, VeoAccount)

    def list_veo_accounts_for_user(self, user_id: str, *, active_only: bool = False) -> List[VeoAccount]:
        """Liệt kê tất cả Veo accounts đã chỉ định cho user."""
        if not user_id:
            return []
        with self._lock:
            assignments = self._read_assignment_map_unlocked()
            ids = assignments.get(user_id) or []
            if ids:
                query = {"id": {"$in": ids}}
                if active_only:
                    query["is_active"] = True
                docs = self._veo_accounts.find(query).sort("name", 1)
                return [obj for obj in (self._doc_to_obj(d, VeoAccount) for d in docs) if obj is not None]

            # Fallback cho dữ liệu cũ còn nằm trong Mongo.
            query = {"assigned_to_user_id": user_id}
            if active_only:
                query["is_active"] = True
            docs = self._veo_accounts.find(query).sort("name", 1)
            return [obj for obj in (self._doc_to_obj(d, VeoAccount) for d in docs) if obj is not None]

    def set_veo_accounts_for_user(self, user_id: str, account_ids: List[str]) -> dict:
        """Gán chính xác danh sách account_ids cho user_id, bỏ các gán cũ không còn chọn."""
        if not user_id:
            return {"assigned": 0, "unassigned": 0, "account_ids": []}
        clean_ids = [str(x).strip() for x in (account_ids or []) if str(x).strip()]
        # Giữ thứ tự nhưng bỏ trùng.
        clean_ids = list(dict.fromkeys(clean_ids))
        with self._lock:
            assignments = self._read_assignment_map_unlocked()
            previous = assignments.get(user_id) or []
            legacy_docs = list(self._veo_accounts.find(
                {"assigned_to_user_id": user_id},
                {"id": 1},
            ))
            legacy_ids = [str(doc.get("id") or "").strip() for doc in legacy_docs if doc.get("id")]
            previous_all = list(dict.fromkeys(previous + legacy_ids))
            previous_set = set(previous_all)
            requested_set = set(clean_ids)

            # Không cho cướp account đang được user khác giữ trong file.
            owner_by_account = {
                account_id: owner_id
                for owner_id, ids in assignments.items()
                for account_id in ids
            }
            allowed_ids = [
                account_id for account_id in clean_ids
                if owner_by_account.get(account_id) in (None, user_id)
            ]

            if allowed_ids:
                assignments[user_id] = allowed_ids
            else:
                assignments.pop(user_id, None)
            self._write_assignment_map_unlocked(assignments)

            new_set = set(allowed_ids)
            removed_ids = list(previous_set - new_set)
            if removed_ids:
                self._veo_accounts.update_many(
                    {"id": {"$in": removed_ids}, "assigned_to_user_id": user_id},
                    {"$unset": {"assigned_to_user_id": ""}},
                )
            # Dọn mọi field legacy còn treo của user này không nằm trong danh sách mới.
            self._veo_accounts.update_many(
                {
                    "assigned_to_user_id": user_id,
                    "id": {"$nin": allowed_ids},
                },
                {"$unset": {"assigned_to_user_id": ""}},
            )
            if allowed_ids:
                self._veo_accounts.update_many(
                    {"id": {"$in": allowed_ids}},
                    {"$set": {"assigned_to_user_id": user_id}},
                )

            return {
                "assigned": len(new_set - previous_set),
                "unassigned": len(previous_set - new_set),
                "account_ids": allowed_ids,
                "skipped": len(requested_set - new_set),
            }

    def update_veo_account_api_session(self, account_id: str, session_data: dict) -> bool:
        """Lưu metadata lần cấp token/project gần nhất cho account."""
        if not account_id:
            return False
        with self._lock:
            res = self._veo_accounts.update_one(
                {"id": account_id},
                {"$set": {"api_session": session_data or {}}},
            )
            return res.modified_count > 0

    def unassign_veo_account(self, account_id: str) -> bool:
        """Giải phóng cookie khỏi user đang được gán."""
        with self._lock:
            assignments = self._read_assignment_map_unlocked()
            changed = False
            for user_id, ids in list(assignments.items()):
                if account_id in ids:
                    assignments[user_id] = [x for x in ids if x != account_id]
                    if not assignments[user_id]:
                        assignments.pop(user_id, None)
                    changed = True
            if changed:
                self._write_assignment_map_unlocked(assignments)
            res = self._veo_accounts.update_one(
                {"id": account_id},
                {"$unset": {"assigned_to_user_id": ""}},
            )
            return changed or res.modified_count > 0

    # ─── App Settings ───

    def get_setting(self, key: str, default=None):
        """Lấy giá trị setting từ DB (MongoDB collection 'settings')."""
        with self._lock:
            doc = self._settings.find_one({"key": key})
            return doc["value"] if doc else default

    def set_setting(self, key: str, value) -> None:
        """Lưu giá trị setting vào DB (upsert theo key)."""
        with self._lock:
            self._settings.update_one(
                {"key": key},
                {"$set": {"key": key, "value": value}},
                upsert=True,
            )


# Singleton store dùng chung toàn app
store = MongoStore()
