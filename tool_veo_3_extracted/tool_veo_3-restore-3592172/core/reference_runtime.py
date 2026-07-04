from __future__ import annotations

import atexit
import hashlib
import logging
import sys
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable
import uuid

ROOT_DIR = Path(__file__).resolve().parents[1]
REFERENCE_DIR = ROOT_DIR / "reference-implementation"

logger = logging.getLogger(__name__)

_SCHEDULER_CACHE: dict[tuple, Any] = {}
_SCHEDULER_CACHE_LOCK = threading.RLock()


def _ensure_reference_path() -> None:
    ref = str(REFERENCE_DIR)
    if ref not in sys.path:
        sys.path.insert(0, ref)


def normalize_aspect(value: str | None) -> str:
    if value in ("9:16", "IMAGE_ASPECT_RATIO_PORTRAIT"):
        return "9:16"
    if value in ("1:1", "IMAGE_ASPECT_RATIO_SQUARE"):
        return "1:1"
    return "16:9"


def normalize_mode(value: str | None) -> str:
    value = (value or "image").strip().lower()
    return "video" if value == "video" else "image"


def token_fingerprint(token: str) -> str:
    return hashlib.sha1(token.encode("utf-8", errors="ignore")).hexdigest()[:8]


def _fingerprint_value(value: str | None) -> str:
    value = (value or "").strip()
    return hashlib.sha1(value.encode("utf-8", errors="ignore")).hexdigest()[:12] if value else "direct"


def _normalize_proxy_key(proxy: str | None) -> str:
    value = (proxy or "").strip()
    if not value:
        return ""
    if "://" in value:
        return value
    parts = value.split(":")
    if len(parts) == 4 and all(parts):
        host, port, user, password = parts
        return f"http://{user}:{password}@{host}:{port}"
    return f"http://{value}"


def _patch_reference_project(project_id: str) -> None:
    """Patch PROJECT_ID runtime values in banana_client without editing source.

    The reference implementation uses module-level constants. The API adapter only
    mirrors the project id returned by account-session into those constants before
    constructing the reference scheduler.
    """
    if not project_id:
        return
    import banana_client  # type: ignore

    banana_client.PROJECT_ID = project_id
    target_url = (
        f"https://aisandbox-pa.googleapis.com/v1/projects/{project_id}/flowMedia:batchGenerateImages"
    )
    banana_client.IMAGE_CREATE_URL = target_url
    logger.info(
        "[ReferenceRuntime] request target url=%s token_project=%s",
        target_url,
        project_id,
    )


def _result_to_dict(result: Any) -> dict[str, Any]:
    try:
        return asdict(result)
    except Exception:
        return {
            "job_id": getattr(result, "job_id", None),
            "status": getattr(result, "status", None),
            "prompt": getattr(result, "prompt", None),
            "lane_id": getattr(result, "lane_id", None),
            "token_fingerprint": getattr(result, "token_fingerprint", None),
            "attempts": getattr(result, "attempts", None),
            "download_url": getattr(result, "download_url", None),
            "saved_path": getattr(result, "saved_path", None),
            "error": getattr(result, "error", None),
            "duration_seconds": getattr(result, "duration_seconds", None),
        }


def _scheduler_cache_key(
    *,
    tokens: list[str],
    token_project_map: dict[str, str],
    proxies: list[str | None],
) -> tuple:
    token_parts = []
    for index, token_value in enumerate(tokens):
        project_value = (token_project_map.get(token_value) or "").strip()
        proxy_value = proxies[index] if index < len(proxies) else None
        token_parts.append(
            (
                token_fingerprint(token_value),
                _fingerprint_value(token_value),
                project_value,
                _normalize_proxy_key(proxy_value),
            )
        )
    return tuple(token_parts)


def _get_or_create_scheduler(
    *,
    scheduler_cls,
    cache_key: tuple,
    tokens: list[str],
    token_project_map: dict[str, str],
    proxies: list[str | None],
    thread_count: int,
    max_attempts: int,
    runtime_dir: str,
    refresh_token_callback=None,
    rotate_proxy_callback=None,
):
    with _SCHEDULER_CACHE_LOCK:
        scheduler = _SCHEDULER_CACHE.get(cache_key)
        if scheduler is not None:
            scheduler.max_attempts = max_attempts
            scheduler.token_refresh_callback = refresh_token_callback
            scheduler.proxy_rotate_callback = rotate_proxy_callback
            scheduler.thread_count = max(1, min(int(thread_count or 1), getattr(scheduler, "max_thread_count", thread_count)))
            logger.info(
                "[ReferenceRuntime] action=reuse_scheduler tokens=%s chrome=keep_alive single_chrome_per_token=1 thread_count=%s",
                ",".join(part[0] for part in cache_key),
                scheduler.thread_count,
            )
            return scheduler

        # Create with maximum per-token capacity so all lanes exist once. Each
        # submit can still lower scheduler.thread_count before dispatch.
        per_token_threads = 3
        create_thread_count = max(int(thread_count or 1), len(tokens) * per_token_threads)
        scheduler = scheduler_cls(
            tokens=tokens,
            thread_count=create_thread_count,
            max_attempts=max_attempts,
            runtime_dir=runtime_dir,
            token_project_map=token_project_map,
            proxies=proxies,
            token_refresh_callback=refresh_token_callback,
            proxy_rotate_callback=rotate_proxy_callback,
        )
        scheduler.thread_count = max(1, min(int(thread_count or 1), getattr(scheduler, "max_thread_count", create_thread_count)))
        _SCHEDULER_CACHE[cache_key] = scheduler
        logger.info(
            "[ReferenceRuntime] action=create_scheduler tokens=%s chrome=keep_alive single_chrome_per_token=1 thread_count=%s runtime_dir=%s",
            ",".join(part[0] for part in cache_key),
            scheduler.thread_count,
            runtime_dir,
        )
        return scheduler


def shutdown_reference_runtimes() -> None:
    """Shutdown all cached reference schedulers/Chrome processes."""
    with _SCHEDULER_CACHE_LOCK:
        schedulers = list(_SCHEDULER_CACHE.values())
        _SCHEDULER_CACHE.clear()
    for scheduler in schedulers:
        try:
            scheduler.shutdown()
        except Exception:
            logger.exception("[ReferenceRuntime] cached scheduler shutdown failed")


atexit.register(shutdown_reference_runtimes)


def run_reference_jobs(
    *,
    token: str,
    project_id: str,
    prompts: Iterable[str],
    mode: str = "image",
    model: str = "GEM_PIX_2",
    aspect_ratio: str = "16:9",
    thread_count: int = 1,
    max_attempts: int = 5,
    output_dir: str | None = None,
    reference_paths: Iterable[str | None] | None = None,
    end_reference_paths: Iterable[str | None] | None = None,
    video_type: str = "single",
    image_resolution: str = "1K",
    output_prefix: str | None = None,
    runtime_session_id: str | None = None,
    result_callback=None,
    refresh_token_callback=None,
    rotate_proxy_callback=None,
    tokens: Iterable[str] | None = None,
    token_project_map: dict[str, str] | None = None,
    proxies: Iterable[str | None] | None = None,
) -> list[dict[str, Any]]:
    """Map API data into reference BananaJob objects and call BananaScheduler.

    Scheduler/Chrome runtimes are cached per token/project/proxy so one token
    keeps one Chrome process across batches for the lifetime of this server.
    """
    _ensure_reference_path()
    _patch_reference_project(project_id)

    from scheduler import BananaJob, BananaScheduler  # type: ignore

    clean_prompts = [str(p).strip() for p in prompts if str(p).strip()]

    def _path_value(item: Any) -> str | None:
        if not item:
            return None
        if isinstance(item, dict):
            item = (
                item.get("path")
                or item.get("saved_path")
                or item.get("file_path")
                or item.get("local_path")
                or item.get("url")
                or item.get("download_url")
            )
        return str(item).strip() if item else None

    refs = [_path_value(item) for item in (reference_paths or [])]
    end_refs = [_path_value(item) for item in (end_reference_paths or [])]
    if not clean_prompts:
        raise ValueError("prompts is required")

    normalized_mode = normalize_mode(mode)
    normalized_aspect = normalize_aspect(aspect_ratio)
    base_output = Path(output_dir or (ROOT_DIR / "outputs" / "api_reference"))
    base_output.mkdir(parents=True, exist_ok=True)

    extension = "jpg" if normalized_mode == "image" else "mp4"
    jobs = []
    for index, prompt in enumerate(clean_prompts, start=1):
        ref = refs[index - 1] if index - 1 < len(refs) else None
        end_ref = end_refs[index - 1] if index - 1 < len(end_refs) else None
        safe_prefix = "".join(
            c if c.isalnum() or c in {"-", "_"} else "_"
            for c in str(output_prefix or "").strip()
        ).strip("_")
        unique_suffix = uuid.uuid4().hex[:8]
        filename = (
            f"{safe_prefix}_{index}_{unique_suffix}.{extension}"
            if safe_prefix
            else f"banana_{uuid.uuid4().hex[:12]}_{index}_{unique_suffix}.{extension}"
        )
        jobs.append(
            BananaJob(
                mode=normalized_mode,
                prompt=prompt,
                model=model or "GEM_PIX_2",
                aspect_ratio=normalized_aspect,
                reference_path=ref or None,
                output_path=str(base_output / filename),
                video_type=video_type or "single",
                end_reference_path=end_ref if (video_type or "single") == "start_end" else None,
                image_resolution=image_resolution or "1K",
            )
        )

    safe_thread_count = max(1, int(thread_count or 1))
    safe_max_attempts = max(1, int(max_attempts or 5))
    scheduler_tokens = [str(t).strip() for t in (tokens or [token]) if str(t).strip()]
    scheduler_project_map = token_project_map or {scheduler_tokens[0]: project_id}
    scheduler_proxies = list(proxies or [])
    cache_key = _scheduler_cache_key(
        tokens=scheduler_tokens,
        token_project_map=scheduler_project_map,
        proxies=scheduler_proxies,
    )
    safe_session_id = "reference_runtime_keepalive_" + hashlib.sha1(repr(cache_key).encode("utf-8", errors="ignore")).hexdigest()[:16]
    runtime_dir = str(REFERENCE_DIR / ".runtime" / "projects" / safe_session_id)
    Path(runtime_dir).mkdir(parents=True, exist_ok=True)

    logger.info(
        "[ReferenceRuntime] direct submit token=%s project=%s mode=%s jobs=%s thread_count=%s max_attempts=%s runtime_session=%s runtime_dir=%s",
        token_fingerprint(token),
        project_id,
        normalized_mode,
        len(jobs),
        safe_thread_count,
        safe_max_attempts,
        safe_session_id,
        runtime_dir,
    )
    logger.info(
        "[ReferenceRuntime] outgoing request method=POST url=%s authorization=Bearer:%s project=%s jobs=%s mode=%s",
        f"https://aisandbox-pa.googleapis.com/v1/projects/{project_id}/flowMedia:batchGenerateImages",
        token_fingerprint(token),
        project_id,
        len(jobs),
        normalized_mode,
    )
    for job in jobs:
        logger.info(
            "[ReferenceJob] job=%s mode=%s model=%s aspect=%s video_type=%s ref=%s end_ref=%s image_resolution=%s output=%s",
            getattr(job, "job_id", None),
            getattr(job, "mode", None),
            getattr(job, "model", None),
            getattr(job, "aspect_ratio", None),
            getattr(job, "video_type", None),
            getattr(job, "reference_path", None),
            getattr(job, "end_reference_path", None),
            getattr(job, "image_resolution", None),
            getattr(job, "output_path", None),
        )

    scheduler = _get_or_create_scheduler(
        scheduler_cls=BananaScheduler,
        cache_key=cache_key,
        tokens=scheduler_tokens,
        token_project_map=scheduler_project_map,
        proxies=scheduler_proxies,
        thread_count=safe_thread_count,
        max_attempts=safe_max_attempts,
        runtime_dir=runtime_dir,
        refresh_token_callback=refresh_token_callback,
        rotate_proxy_callback=rotate_proxy_callback,
    )
    previous_callback = getattr(scheduler, "result_callback", None)
    if result_callback is not None:
        scheduler.result_callback = result_callback
    try:
        return [_result_to_dict(result) for result in scheduler.submit(jobs)]
    finally:
        scheduler.result_callback = previous_callback
