"""
project_tracker.py — Background thread auto-detect khi project hoàn tất
task cuối cùng → log + persist `started_at` / `finished_at` vào Project DB.

Logic:
  - Poll 30s/lần, scan all projects.
  - Project được coi là HOÀN TẤT khi:
      total_tasks > 0
      pending = 0 (tính cả PROCESSING)
      paused = 0 (paused thì user còn ý định resume → chưa coi là xong)
  - started_at = min(task.created_at) trong project
  - finished_at = max(task.completed_at) trong project (terminal status)
  - Dedupe in-memory: project đã log rồi → bỏ qua.
  - Reset dedup khi project có pending/processing trở lại (user thêm batch
    mới) → lần xong tiếp sẽ log lại.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

POLL_INTERVAL = 30  # giây
TERMINAL_STATUSES = {"COMPLETED", "FAILED", "ERROR"}


class ProjectTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self._logged_finished_ids: set[str] = set()
        self._seeded_existing_finished = False
        self._started = False

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        t = threading.Thread(target=self._loop, daemon=True, name="ProjectTracker")
        t.start()
        logger.info("[ProjectTracker] Started — poll every %ds", POLL_INTERVAL)

    def _loop(self) -> None:
        while True:
            time.sleep(POLL_INTERVAL)
            try:
                self._scan_once()
            except Exception as e:
                logger.warning("[ProjectTracker] scan error: %s", e)

    def _scan_once(self) -> None:
        # Lazy import để tránh circular import
        from web import store
        try:
            projects = store.list_projects()
        except Exception as e:
            logger.warning("[ProjectTracker] list_projects fail: %s", e)
            return
        try:
            stats = store.get_all_project_stats()
        except Exception as e:
            logger.warning("[ProjectTracker] get_all_project_stats fail: %s", e)
            return

        if not self._seeded_existing_finished:
            for p in projects:
                pid = getattr(p, "id", None)
                if pid and getattr(p, "finished_at", None):
                    self._logged_finished_ids.add(pid)
            self._seeded_existing_finished = True

        for p in projects:
            pid = getattr(p, "id", None)
            if not pid:
                continue
            s = stats.get(pid, {})
            completed = s.get("COMPLETED", 0)
            failed = s.get("FAILED", 0) + s.get("ERROR", 0)
            pending = s.get("PENDING", 0) + s.get("PROCESSING", 0)
            paused = s.get("PAUSED", 0)
            total = completed + failed + pending + paused

            # Project có pending/processing trở lại → reset dedup
            # (user thêm batch mới → lần kế tiếp xong sẽ log lại)
            if pending > 0 or paused > 0:
                self._logged_finished_ids.discard(pid)
                continue

            if total <= 0:
                continue

            if pid in self._logged_finished_ids:
                continue

            # Project cũ đã có finished_at trong DB thì không load 20k task để log lại.
            if getattr(p, "finished_at", None):
                self._logged_finished_ids.add(pid)
                continue

            # Project đã hoàn tất hết tasks → compute started_at + finished_at
            self._finalize_project(p, completed, failed, total)

    def _finalize_project(self, project, completed: int, failed: int, total: int) -> None:
        from web import store
        try:
            tasks, _ = store.list_video_tasks(project_id=project.id, limit=20000)
        except Exception as e:
            logger.warning("[ProjectTracker] list_video_tasks(%s) fail: %s", project.id, e)
            return

        starts: list[float] = []
        ends: list[float] = []
        for t in tasks:
            ca = getattr(t, "created_at", None)
            if ca:
                try:
                    starts.append(float(ca.timestamp() if hasattr(ca, "timestamp") else ca))
                except Exception:
                    pass
            comp = getattr(t, "completed_at", None) or getattr(t, "updated_at", None)
            if comp:
                try:
                    ends.append(float(comp.timestamp() if hasattr(comp, "timestamp") else comp))
                except Exception:
                    pass

        if not starts or not ends:
            logger.info(
                "[ProjectTracker] Project %s không đủ timestamp (starts=%d ends=%d) — skip",
                project.id, len(starts), len(ends),
            )
            self._logged_finished_ids.add(project.id)
            return

        started_at = min(starts)
        finished_at = max(ends)
        duration = max(0.0, finished_at - started_at)
        rate = (completed + failed) * 60.0 / duration if duration > 0 else 0

        # Persist vào Project DB
        try:
            project.started_at = started_at
            project.finished_at = finished_at
            store.update_project(project)
        except Exception as e:
            logger.warning("[ProjectTracker] update_project(%s) fail: %s", project.id, e)

        logger.info(
            "[Project %s \"%s\"] HOÀN THÀNH | "
            "Bắt đầu=%s | Kết thúc=%s | Duration=%s | "
            "%d task: %d ✅ / %d ❌ | rate=%.1f/min",
            project.id[:8],
            (getattr(project, "name", "") or "")[:30],
            time.strftime("%H:%M:%S %d/%m", time.localtime(started_at)),
            time.strftime("%H:%M:%S %d/%m", time.localtime(finished_at)),
            _fmt_dur(duration),
            total, completed, failed, rate,
        )
        self._logged_finished_ids.add(project.id)
        try:
            from core.chrome_cleanup import cleanup_project
            cleanup_project(project.id, reason="project_completed")
        except Exception as e:
            logger.warning("[ProjectTracker] chrome cleanup failed for %s: %s", project.id, e)


def _fmt_dur(sec: float) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


# Singleton
_TRACKER = ProjectTracker()


def start() -> None:
    _TRACKER.start()
