import logging
import os
import threading
from pathlib import Path
from typing import Iterable

from core.banana_runtime.scheduler import BananaJob, BananaScheduler
from web.models import TaskStatus, VideoTask
from web.store import store


logger = logging.getLogger(__name__)


def split_access_tokens(value: str | Iterable[str]) -> list[str]:
    if isinstance(value, str):
        raw_parts = value.replace(",", "\n").splitlines()
    else:
        raw_parts = list(value)
    return [part.strip() for part in raw_parts if part and str(part).strip()]


def resolve_tokens_from_pool() -> list[str]:
    """Auto-fetch access tokens from active VeoAccount pool.

    Uses a lightweight requests.get to the Google Labs session endpoint
    with each account's cookie — avoids heavy ImagenClient / curl_cffi init.
    """
    import requests as _requests
    SESSION_URL = "https://labs.google/fx/api/auth/session"

    tokens: list[str] = []
    try:
        accounts = store.list_veo_accounts()
    except Exception as exc:
        logger.warning("[BananaAdapter] Cannot list VeoAccounts: %s", exc)
        return tokens

    active = [a for a in accounts if getattr(a, "is_active", False)]
    if not active:
        logger.warning("[BananaAdapter] No active VeoAccounts in pool")
        return tokens

    for acc in active:
        try:
            cookie_str = getattr(acc, "cookie", "") or ""
            if not cookie_str:
                continue
            proxy_str = getattr(acc, "static_proxy", None) or getattr(acc, "proxy", None)
            proxies = None
            if proxy_str:
                p = proxy_str if "://" in proxy_str else f"http://{proxy_str}"
                proxies = {"http": p, "https": p}

            import json
            cookie_header = cookie_str.strip()
            if cookie_header.startswith("[") or cookie_header.startswith("{"):
                try:
                    c_obj = json.loads(cookie_header)
                    if isinstance(c_obj, list):
                        parts = [f"{c['name']}={c['value']}" for c in c_obj if isinstance(c, dict) and "name" in c and "value" in c]
                        if parts:
                            cookie_header = "; ".join(parts)
                    elif isinstance(c_obj, dict) and "value" in c_obj:
                        cookie_header = c_obj["value"]
                except Exception:
                    pass
            
            if cookie_header.startswith("ey") and "=" not in cookie_header[:20]:
                cookie_header = f"__Secure-next-auth.session-token={cookie_header}"

            headers = {
                "Cookie": cookie_header,
                "referer": "https://labs.google/fx/vi/tools/flow",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
            }

            resp = _requests.get(
                SESSION_URL,
                headers=headers,
                proxies=proxies,
                timeout=10,
                verify=False,
            )
            if resp.status_code == 200:
                data = resp.json()
                token = data.get("access_token")
                if token:
                    tokens.append(token)
                    logger.info("[BananaAdapter] Token OK from %s: %s...", acc.name, token[:20])
                else:
                    logger.warning("[BananaAdapter] No token in response for %s", acc.name)
            else:
                logger.warning("[BananaAdapter] Session %d for %s", resp.status_code, acc.name)
        except Exception as exc:
            logger.warning("[BananaAdapter] Token fetch failed for %s: %s", acc.name, exc)

    logger.info("[BananaAdapter] Resolved %d token(s) from %d active account(s)", len(tokens), len(active))
    return tokens


def normalize_banana_aspect(value: str) -> str:
    if value in ("IMAGE_ASPECT_RATIO_LANDSCAPE", "16:9"):
        return "16:9"
    if value in ("IMAGE_ASPECT_RATIO_SQUARE", "1:1"):
        return "1:1"
    return "9:16"


def first_reference_path(image_refs) -> str | None:
    if not image_refs:
        return None
    first = image_refs[0]
    if isinstance(first, dict):
        return first.get("path")
    if isinstance(first, str):
        return first
    return None


def dispatch_banana_tasks(
    tasks: list[VideoTask],
    access_tokens: str | Iterable[str],
    thread_count: int,
    output_dir: str,
    max_attempts: int = 5,
) -> threading.Thread:
    thread = threading.Thread(
        target=_run_banana_tasks,
        args=(tasks, access_tokens, thread_count, output_dir, max_attempts),
        name="BananaSchedulerAdapter",
        daemon=True,
    )
    thread.start()
    return thread


def _run_banana_tasks(
    tasks: list[VideoTask],
    access_tokens: str | Iterable[str],
    thread_count: int,
    output_dir: str,
    max_attempts: int = 5,
) -> None:
    tokens = split_access_tokens(access_tokens)
    if not tokens:
        logger.info("[BananaAdapter] No tokens provided, resolving from VeoAccount pool...")
        tokens = resolve_tokens_from_pool()
    if not tokens:
        _fail_all(tasks, "No access tokens available (none provided and none resolved from account pool)")
        return

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    jobs: list[BananaJob] = []
    job_to_task_id: dict[str, str] = {}
    for task in tasks:
        task.status = TaskStatus.PROCESSING
        store.update_video_task(task)

        ref_path = first_reference_path(task.image_refs)
        requested_count = max(1, int(getattr(task, "count", 1) or 1))
        for index in range(requested_count):
            job_id = task.id if requested_count == 1 else f"{task.id}__{index + 1}"
            output_path = os.path.join(output_dir, f"banana_{task.id[:8]}_{index + 1}.jpg")
            jobs.append(
                BananaJob(
                    prompt=(task.prompts or [""])[0],
                    model=task.model,
                    aspect_ratio=normalize_banana_aspect(task.screen_ratio),
                    reference_path=ref_path,
                    output_path=output_path,
                    job_id=job_id,
                )
            )
            job_to_task_id[job_id] = task.id

    scheduler = None
    try:
        scheduler = BananaScheduler(
            tokens=tokens,
            thread_count=max(1, int(thread_count or 1)),
            max_attempts=max(1, int(max_attempts or 5)),
        )
        results = scheduler.submit(jobs)
    except Exception as exc:
        logger.exception("[BananaAdapter] Scheduler failed: %s", exc)
        _fail_all(tasks, str(exc))
        return
    finally:
        if scheduler is not None:
            scheduler.shutdown()

    task_by_id = {task.id: task for task in tasks}
    grouped: dict[str, list] = {task.id: [] for task in tasks}
    for result in results:
        task_id = job_to_task_id.get(result.job_id, result.job_id)
        grouped.setdefault(task_id, []).append(result)

    now = __import__("time").time
    for task_id, task_results in grouped.items():
        task = task_by_id.get(task_id)
        if not task:
            continue

        completed = [r for r in task_results if r.status == "completed"]
        failed = [r for r in task_results if r.status != "completed"]
        image_paths = [r.saved_path or r.download_url for r in completed if (r.saved_path or r.download_url)]

        if completed:
            primary = image_paths[0] if image_paths else (completed[0].download_url or completed[0].saved_path)
            task.status = TaskStatus.COMPLETED
            task.media_id = primary
            task.output_filename = os.path.basename(primary) if primary and os.path.exists(primary) else None
            task.error = None if not failed else f"{len(failed)} Banana variation(s) failed"
            task.raw_result = {
                "source": "imagen",
                "backend": "banana",
                "image_paths": image_paths,
                "download_url": completed[0].download_url,
                "results": [
                    {
                        "job_id": r.job_id,
                        "status": r.status,
                        "download_url": r.download_url,
                        "saved_path": r.saved_path,
                        "lane_id": r.lane_id,
                        "token_fingerprint": r.token_fingerprint,
                        "attempts": r.attempts,
                        "duration_seconds": r.duration_seconds,
                        "error": r.error,
                    }
                    for r in task_results
                ],
            }
        else:
            task.status = TaskStatus.FAILED
            task.error = (failed[0].error if failed else None) or "Banana image generation failed"
            task.raw_result = {
                "source": "imagen",
                "backend": "banana",
                "results": [
                    {
                        "job_id": r.job_id,
                        "status": r.status,
                        "lane_id": r.lane_id,
                        "token_fingerprint": r.token_fingerprint,
                        "attempts": r.attempts,
                        "duration_seconds": r.duration_seconds,
                        "error": r.error,
                    }
                    for r in task_results
                ],
            }
        task.completed_at = now()
        store.update_video_task(task)


def _fail_all(tasks: list[VideoTask], message: str) -> None:
    for task in tasks:
        task.status = TaskStatus.FAILED
        task.error = message
        task.raw_result = {"source": "imagen", "backend": "banana", "error": message}
        task.completed_at = __import__("time").time()
        store.update_video_task(task)

