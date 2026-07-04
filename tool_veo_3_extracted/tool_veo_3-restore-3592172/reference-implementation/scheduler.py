import hashlib
import logging
import queue
import random
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from banana_client import BananaImageClient, BananaClientError


REQUEST_DELAY_SECONDS = 0.0
IMAGE_RANDOM_DELAY_MIN_SECONDS = 10.0
IMAGE_RANDOM_DELAY_MAX_SECONDS = 15.0
RATE_LIMIT_429_COOLDOWN_SECONDS = 10.0
SERVER_500_BACKOFF_BASE_SECONDS = 30.0
SERVER_500_BACKOFF_MAX_SECONDS = 120.0
PER_TOKEN_THREADS = 3


def token_fingerprint(token: str) -> str:
    return hashlib.sha1(token.encode('utf-8')).hexdigest()[:8]


def normalize_proxy(proxy: Optional[str]) -> Optional[str]:
    value = (proxy or '').strip()
    if not value:
        return None
    if '://' in value:
        return value
    parts = value.split(':')
    if len(parts) == 4 and all(parts):
        host, port, user, password = parts
        return f'http://{user}:{password}@{host}:{port}'
    return f'http://{value}'


@dataclass
class BananaJob:
    mode: str
    prompt: str
    model: str
    aspect_ratio: str
    reference_path: Optional[str] = None
    output_path: Optional[str] = None
    video_type: str = 'single'
    end_reference_path: Optional[str] = None
    reference_paths: Optional[list[str]] = None
    image_resolution: str = '1K'
    attempts: int = 0
    job_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class BananaJobResult:
    job_id: str
    status: str
    prompt: str
    lane_id: str
    token_fingerprint: str
    attempts: int
    download_url: Optional[str] = None
    saved_path: Optional[str] = None
    error: Optional[str] = None
    duration_seconds: Optional[float] = None


@dataclass
class BananaTokenRuntime:
    token: str
    proxy: Optional[str]
    project_id: Optional[str]
    client: BananaImageClient
    client_lock: threading.RLock = field(default_factory=threading.RLock)
    reset_lock: threading.RLock = field(default_factory=threading.RLock)
    warmup_lock: threading.Lock = field(default_factory=threading.Lock)
    ready_event: threading.Event = field(default_factory=threading.Event)
    consecutive_403: int = 0
    invalid_auth: bool = False
    last_reset_at: float = 0.0
    video_create_lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def token_short(self) -> str:
        return token_fingerprint(self.token)


class BananaLane:
    def __init__(self, lane_id: str, runtime: BananaTokenRuntime):
        self.lane_id = lane_id
        self.runtime = runtime
        self.access_token = runtime.token
        self.project_id = (runtime.project_id or '').strip() or None
        self.token_short = runtime.token_short


_SHARED_RUNTIME_LOCK = threading.RLock()
_SHARED_TOKEN_RUNTIMES: dict[str, BananaTokenRuntime] = {}


def _get_or_create_shared_runtime(
    *,
    token: str,
    proxy: Optional[str],
    project_id: Optional[str],
    profile_dir: Path,
    lane_id: str,
) -> BananaTokenRuntime:
    token_short = token_fingerprint(token)
    with _SHARED_RUNTIME_LOCK:
        runtime = _SHARED_TOKEN_RUNTIMES.get(token_short)
        if runtime is not None:
            with runtime.reset_lock:
                runtime.token = token
                runtime.project_id = project_id
                if proxy and proxy != runtime.proxy:
                    runtime.proxy = proxy
            logging.info(
                'stage=scheduler token=%s chrome=dedicated action=reuse_shared_runtime single_chrome_per_token=1 proxy=%s profile=%s',
                token_short,
                runtime.proxy or 'direct',
                profile_dir,
            )
            return runtime

        runtime = BananaTokenRuntime(
            token=token,
            proxy=proxy,
            project_id=project_id,
            client=BananaImageClient(
                logger=lambda message: logging.info(message),
                lane_id=lane_id,
                user_data_dir=str(profile_dir),
                proxy_server=proxy,
            ),
        )
        _SHARED_TOKEN_RUNTIMES[token_short] = runtime
        return runtime


class BananaScheduler:
    AUTOMATION_403_THRESHOLD = 4
    AUTOMATION_RESET_COOLDOWN_SECONDS = 20.0

    def __init__(
        self,
        tokens: list[str],
        thread_count: int,
        max_attempts: int = 5,
        runtime_dir: Optional[str] = None,
        token_project_map: Optional[dict[str, str]] = None,
        result_callback: Optional[Callable[[BananaJobResult], None]] = None,
        proxies: Optional[list[str]] = None,
        per_token_threads: int = PER_TOKEN_THREADS,
        token_refresh_callback: Optional[Callable[[str, Optional[str]], dict[str, str] | str | None]] = None,
        proxy_rotate_callback: Optional[Callable[[str, Optional[str]], str | None]] = None,
    ):
        cleaned_tokens = [t.strip() for t in tokens if t and t.strip()]
        if not cleaned_tokens:
            raise ValueError('At least one token is required')

        self.base_dir = Path(runtime_dir) if runtime_dir else Path(__file__).resolve().parent / '.runtime'
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.max_attempts = max_attempts
        self.tokens = cleaned_tokens
        self.token_project_map = token_project_map or {}
        self.proxies = [normalize_proxy(p) for p in (proxies or [])]
        self.per_token_threads = max(1, int(per_token_threads or PER_TOKEN_THREADS))
        self.max_thread_count = max(1, len(cleaned_tokens) * self.per_token_threads)
        requested_thread_count = max(1, int(thread_count or self.max_thread_count))
        self.thread_count = min(requested_thread_count, self.max_thread_count)
        self.result_callback = result_callback
        self.token_refresh_callback = token_refresh_callback
        self.proxy_rotate_callback = proxy_rotate_callback
        self.submit_lock = threading.RLock()
        self.job_queue: queue.Queue[BananaJob] = queue.Queue()
        self.results: list[BananaJobResult] = []
        self.results_lock = threading.Lock()
        self.rate_limit_lock = threading.Lock()
        self.rate_limit_until = 0.0

        self.runtimes: list[BananaTokenRuntime] = []
        for index, token in enumerate(cleaned_tokens):
            token_short = token_fingerprint(token)
            proxy = self.proxies[index] if index < len(self.proxies) else None
            profile_dir = self.base_dir / f'token_{index + 1}_{token_short}' / 'profile'
            runtime = _get_or_create_shared_runtime(
                token=token,
                proxy=proxy,
                project_id=self.token_project_map.get(token),
                profile_dir=profile_dir,
                lane_id=f'token-{index + 1}-{token_short}',
            )
            self.runtimes.append(runtime)
            logging.info(
                'stage=scheduler token=%s chrome=dedicated single_chrome_per_token=1 proxy=%s per_token_threads=%s profile=%s',
                token_short,
                proxy or 'direct',
                self.per_token_threads,
                profile_dir,
            )

        self.lanes: list[BananaLane] = []
        for index in range(self.thread_count):
            runtime = self.runtimes[(index // self.per_token_threads) % len(self.runtimes)]
            lane_number_for_token = (index % self.per_token_threads) + 1
            lane = BananaLane(f'token-{runtime.token_short}-lane-{lane_number_for_token}', runtime)
            self.lanes.append(lane)
            logging.info(
                'stage=scheduler lane=%s token=%s chrome=shared_token_runtime single_chrome_per_token=1',
                lane.lane_id,
                runtime.token_short,
            )

    def _delay_before_request(self, stage: str) -> None:
        return

    def _delay_before_create_submit(self, job: BananaJob, lane: BananaLane) -> None:
        if job.mode == 'image':
            logging.info('lane=%s job=%s stage=image-submit-delay action=skip_sleep', lane.lane_id, job.job_id)
        else:
            logging.info('lane=%s job=%s stage=video-submit-wait action=skip_sleep', lane.lane_id, job.job_id)

    def submit(self, jobs: list[BananaJob]) -> list[BananaJobResult]:
        if not jobs:
            return []

        # A cached scheduler owns long-lived token Chrome runtimes. Keep submits
        # sequential so queue/results from two API batches cannot interleave.
        with self.submit_lock:
            self.job_queue = queue.Queue()
            with self.results_lock:
                self.results = []

            for job in jobs:
                self.job_queue.put(job)

            workers = []
            actual_worker_count = min(self.thread_count, len(jobs))
            for worker_id in range(1, actual_worker_count + 1):
                thread = threading.Thread(target=self._worker_loop, args=(worker_id,), daemon=True)
                thread.start()
                workers.append(thread)

            self.job_queue.join()

            for _ in workers:
                self.job_queue.put(None)  # type: ignore[arg-type]
            for worker in workers:
                worker.join()

            with self.results_lock:
                return list(self.results)

    def shutdown(self) -> None:
        for runtime in self.runtimes:
            try:
                logging.info('stage=scheduler token=%s chrome=dedicated action=keep_shared_runtime_alive single_chrome_per_token=1', runtime.token_short)
            except Exception:
                pass

    def _reset_runtime_browser(self, runtime: BananaTokenRuntime, reason: str) -> None:
        with runtime.reset_lock:
            now = time.time()
            recently_reset = (now - runtime.last_reset_at) < self.AUTOMATION_RESET_COOLDOWN_SECONDS
            if recently_reset:
                logging.warning(
                    'stage=reset scope=token-browser token=%s reason=%s skipped=cooldown remaining=%.2fs',
                    runtime.token_short,
                    reason,
                    self.AUTOMATION_RESET_COOLDOWN_SECONDS - (now - runtime.last_reset_at),
                )
                return

            logging.warning('stage=reset scope=token-browser token=%s action=clear-data reason=%s', runtime.token_short, reason)
            with runtime.client_lock:
                try:
                    runtime.client.clear_browser_data()
                    runtime.ready_event.set()
                except Exception as exc:
                    logging.warning(
                        'stage=reset scope=token-browser token=%s action=clear-data-failed fallback=shutdown error=%s',
                        runtime.token_short,
                        exc,
                    )
                    runtime.ready_event.clear()
                    try:
                        runtime.client.shutdown(remove_profile=False)
                    except Exception as shutdown_exc:
                        logging.warning(
                            'stage=reset scope=token-browser token=%s action=shutdown-failed error=%s',
                            runtime.token_short,
                            shutdown_exc,
                        )
            runtime.last_reset_at = time.time()
            runtime.consecutive_403 = 0

    def _warmup_runtime(self, runtime: BananaTokenRuntime, lane_id: str) -> None:
        if runtime.ready_event.is_set():
            logging.info('lane=%s token=%s stage=warmup action=reuse_ready_chrome single_chrome_per_token=1', lane_id, runtime.token_short)
            return
        with runtime.warmup_lock:
            if runtime.ready_event.is_set():
                logging.info('lane=%s token=%s stage=warmup action=reuse_ready_chrome single_chrome_per_token=1', lane_id, runtime.token_short)
                return
            logging.info(
                'lane=%s token=%s stage=warmup action=start proxy=%s',
                lane_id,
                runtime.token_short,
                runtime.proxy or 'direct',
            )
            with runtime.client_lock:
                runtime.client.browser.ensure_ready()
            runtime.ready_event.set()
            logging.info('lane=%s token=%s stage=warmup action=ready chrome=dedicated single_chrome_per_token=1', lane_id, runtime.token_short)

    def _wait_for_rate_limit_cooldown(self) -> None:
        with self.rate_limit_lock:
            wait_seconds = self.rate_limit_until - time.time()
            if wait_seconds > 0:
                logging.warning('stage=429 action=skip_sleep cooldown_remaining=%.2fs', wait_seconds)
                self.rate_limit_until = 0.0

    def _apply_429_cooldown(self, message: str) -> None:
        logging.warning('stage=429 action=no_global_sleep_retry error=%s', message)

    def _is_429_error(self, message: str) -> bool:
        lowered = (message or '').lower()
        return '429' in lowered or 'too many requests' in lowered or 'resource_exhausted' in lowered

    def _is_500_error(self, message: str) -> bool:
        lowered = (message or '').lower()
        return 'http 500' in lowered or 'internal error encountered' in lowered or 'internal server error' in lowered

    def _apply_500_backoff(self, job: BananaJob, lane: BananaLane, message: str) -> None:
        logging.warning(
            'lane=%s job=%s token=%s attempt=%s stage=server_500_backoff action=no_sleep_retry_via_proxy_rotation error=%s',
            lane.lane_id,
            job.job_id,
            lane.token_short,
            job.attempts,
            message,
        )

    def _rotate_runtime_proxy_for_500(self, job: BananaJob, lane: BananaLane, message: str) -> bool:
        runtime = lane.runtime
        if not self.proxy_rotate_callback:
            logging.warning(
                'lane=%s job=%s token=%s attempt=%s stage=proxy_rotate_on_500 action=no_callback',
                lane.lane_id,
                job.job_id,
                lane.token_short,
                job.attempts,
            )
            return False
        with runtime.reset_lock:
            current_token = runtime.token
            current_project = runtime.project_id
        try:
            rotated_proxy = self.proxy_rotate_callback(current_token, current_project)
        except Exception as exc:
            logging.warning(
                'lane=%s job=%s token=%s attempt=%s stage=proxy_rotate_on_500 action=callback_failed error=%s',
                lane.lane_id,
                job.job_id,
                lane.token_short,
                job.attempts,
                exc,
            )
            return False
        rotated_proxy = normalize_proxy(rotated_proxy)
        if not rotated_proxy:
            logging.warning(
                'lane=%s job=%s token=%s attempt=%s stage=proxy_rotate_on_500 action=cooldown_or_empty',
                lane.lane_id,
                job.job_id,
                lane.token_short,
                job.attempts,
            )
            return False
        with runtime.reset_lock:
            old_proxy = runtime.proxy or 'direct'
            runtime.proxy = rotated_proxy
            runtime.ready_event.clear()
            runtime.last_reset_at = 0.0
        with runtime.client_lock:
            try:
                runtime.client.shutdown(remove_profile=False)
            except Exception as exc:
                logging.warning(
                    'lane=%s job=%s token=%s stage=proxy_rotate_on_500 action=shutdown_failed error=%s',
                    lane.lane_id,
                    job.job_id,
                    lane.token_short,
                    exc,
                )
            runtime.client.proxy_server = rotated_proxy
            try:
                browser = runtime.client.browser
                browser.proxy_server = rotated_proxy
                split_proxy = getattr(browser, "_proxy_server", None)
                # FlowBrowser stores proxy launch/auth components separately.
                # Refresh them so the next ensure_ready() opens Chrome with the
                # newly rotated proxy for this same token.
                if hasattr(browser, "_proxy_server"):
                    from banana_client import split_proxy_components  # type: ignore
                    browser._proxy_server, browser._proxy_username, browser._proxy_password = split_proxy_components(rotated_proxy)
            except Exception as exc:
                logging.warning(
                    'lane=%s job=%s token=%s stage=proxy_rotate_on_500 action=update_browser_proxy_failed error=%s',
                    lane.lane_id,
                    job.job_id,
                    lane.token_short,
                    exc,
                )
        logging.warning(
            'lane=%s job=%s token=%s attempt=%s stage=server_500_backoff action=rotate_proxy_on_500 old_proxy=%s new_proxy=%s',
            lane.lane_id,
            job.job_id,
            lane.token_short,
            job.attempts,
            old_proxy,
            rotated_proxy,
        )
        if self.token_refresh_callback:
            try:
                refreshed = self.token_refresh_callback(current_token, current_project)
                self._apply_refreshed_token(runtime, refreshed)
            except Exception as exc:
                logging.warning(
                    'lane=%s job=%s token=%s stage=proxy_rotate_on_500 action=token_refresh_failed error=%s',
                    lane.lane_id,
                    job.job_id,
                    lane.token_short,
                    exc,
                )
        return True

    def _is_403_error(self, message: str) -> bool:
        return any(text in message for text in ('HTTP 403', 'PERMISSION_DENIED', 'reCAPTCHA evaluation failed', '403 Client Error'))

    def _is_401_error(self, message: str) -> bool:
        lowered = (message or '').lower()
        return '401' in lowered or 'unauthorized' in lowered or 'invalid authentication credentials' in lowered

    def _is_model_access_denied(self, message: str) -> bool:
        return 'PUBLIC_ERROR_MODEL_ACCESS_DENIED' in message or 'MODEL_ACCESS_DENIED' in message

    def _is_transient_browser_context_error(self, message: str) -> bool:
        return any(
            text in message
            for text in (
                'Target closed',
                'Execution context was destroyed',
                'reCAPTCHA not ready',
                'reCAPTCHA library not loaded',
                'Flow page navigation failed',
                'Browser evaluate timeout after 60s',
                'Cannot find context',
                'Session closed',
                'detached Frame',
                'Protocol error',
            )
        )

    def _handle_failure(self, lane: BananaLane, message: str) -> None:
        runtime = lane.runtime
        if self._is_403_error(message):
            if self._is_model_access_denied(message):
                logging.warning('lane=%s token=%s stage=403 reason=model_access_denied action=no_reset', lane.lane_id, lane.token_short)
                return

            with runtime.reset_lock:
                runtime.consecutive_403 += 1
                current_total = runtime.consecutive_403
            logging.warning(
                'lane=%s token=%s stage=403 total=%s/%s lane_count=%s',
                lane.lane_id,
                lane.token_short,
                current_total,
                self.AUTOMATION_403_THRESHOLD,
                runtime.consecutive_403,
            )
            if current_total >= self.AUTOMATION_403_THRESHOLD:
                logging.warning('stage=403 threshold_reached=%s action=reset_token_browser token=%s', self.AUTOMATION_403_THRESHOLD, lane.token_short)
                self._reset_runtime_browser(runtime, '403 threshold')
            else:
                logging.warning('stage=403 action=wait_for_threshold token=%s', lane.token_short)
            return

        if self._is_transient_browser_context_error(message):
            logging.warning('stage=browser_context_error action=reset_token_browser token=%s message=%s', lane.token_short, message)
            self._reset_runtime_browser(runtime, message)

    def _apply_refreshed_token(self, runtime: BananaTokenRuntime, refreshed: dict[str, str] | str | None) -> bool:
        if not refreshed:
            return False
        if isinstance(refreshed, dict):
            new_token = (refreshed.get('token') or '').strip()
            new_project_id = (refreshed.get('project_id') or '').strip() or runtime.project_id
        else:
            new_token = str(refreshed).strip()
            new_project_id = runtime.project_id
        if not new_token:
            return False

        old_short = runtime.token_short
        with runtime.reset_lock:
            runtime.token = new_token
            runtime.project_id = new_project_id
            runtime.invalid_auth = False
        for lane in self.lanes:
            if lane.runtime is runtime:
                lane.access_token = new_token
                lane.project_id = (new_project_id or '').strip() or None
                lane.token_short = runtime.token_short
        logging.warning(
            'stage=poll_401 action=token_refreshed old_token=%s new_token=%s project_id=%s',
            old_short,
            runtime.token_short,
            new_project_id or '',
        )
        return True

    def _refresh_runtime_token_for_poll(self, runtime: BananaTokenRuntime, lane_id: str) -> bool:
        if not self.token_refresh_callback:
            logging.warning('lane=%s token=%s stage=poll_401 action=no_refresh_callback', lane_id, runtime.token_short)
            return False
        with runtime.reset_lock:
            current_token = runtime.token
            project_id = runtime.project_id
        logging.warning('lane=%s token=%s stage=poll_401 action=refresh_token', lane_id, token_fingerprint(current_token))
        try:
            refreshed = self.token_refresh_callback(current_token, project_id)
        except Exception as exc:
            logging.warning('lane=%s token=%s stage=poll_401 action=refresh_failed error=%s', lane_id, token_fingerprint(current_token), exc)
            return False
        return self._apply_refreshed_token(runtime, refreshed)

    def _poll_operation_with_token_refresh(
        self,
        lane: BananaLane,
        operation_name: str,
        scene_id: str,
        failure_message: str,
        include_link: bool = False,
        mode: str = 'image',
    ) -> Optional[dict]:
        runtime = lane.runtime
        client = runtime.client
        refreshed_after_401 = False
        for poll_attempt in range(1, 31):
            time.sleep(7.0)
            logging.info(
                'lane=%s token=%s stage=poll action=sample_like poll_attempt=%s mode=%s operation=%s',
                lane.lane_id,
                lane.token_short,
                poll_attempt,
                mode,
                operation_name,
            )
            try:
                if mode == 'video':
                    video_status = client.poll_video(
                        lane.access_token,
                        operation_name,
                        project_id=lane.project_id,
                    )
                    if video_status['done'] and video_status['failed']:
                        raise BananaClientError('Video generation failed during async polling')
                    if not video_status['done']:
                        continue

                polled = client.poll_image(lane.access_token, operation_name, scene_id)
            except Exception as exc:
                message = str(exc)
                if self._is_401_error(message) and not refreshed_after_401:
                    logging.warning(
                        'lane=%s token=%s stage=poll_401 operation=%s poll_attempt=%s action=refresh_and_retry',
                        lane.lane_id,
                        lane.token_short,
                        operation_name,
                        poll_attempt,
                    )
                    if self._refresh_runtime_token_for_poll(runtime, lane.lane_id):
                        refreshed_after_401 = True
                        logging.warning(
                            'lane=%s token=%s stage=poll_401 operation=%s action=retry_with_refreshed_token',
                            lane.lane_id,
                            lane.token_short,
                            operation_name,
                        )
                        continue
                raise
            if polled['done'] and not polled['failed'] and polled.get('downloadUrl'):
                result = {'type': 'sync', 'downloadUrl': polled['downloadUrl'], 'raw': polled.get('raw')}
                if include_link:
                    result['link'] = polled['downloadUrl']
                return result
            if polled['done'] and polled['failed']:
                raise BananaClientError(failure_message)
        return None

    def _get_next_available_lane(self, current_lane: BananaLane) -> Optional[BananaLane]:
        if current_lane.runtime.invalid_auth:
            candidates = [lane for lane in self.lanes if not lane.runtime.invalid_auth]
            if not candidates:
                return None
            try:
                current_index = self.lanes.index(current_lane)
            except ValueError:
                current_index = -1
            for offset in range(1, len(self.lanes) + 1):
                candidate = self.lanes[(current_index + offset) % len(self.lanes)]
                if not candidate.runtime.invalid_auth:
                    return candidate
        return current_lane

    def _worker_loop(self, worker_id: int) -> None:
        lane = self.lanes[(worker_id - 1) % len(self.lanes)]
        while True:
            job = self.job_queue.get()
            if job is None:
                self.job_queue.task_done()
                break
            try:
                active_lane = self._get_next_available_lane(lane)
                if active_lane is None:
                    result = BananaJobResult(
                        job_id=job.job_id,
                        status='failed',
                        prompt=job.prompt,
                        lane_id=lane.lane_id,
                        token_fingerprint=lane.token_short,
                        attempts=job.attempts,
                        error='All tokens are invalid due to 401 authentication errors',
                    )
                else:
                    lane = active_lane
                    result = self._run_job_on_lane(job, lane)
                with self.results_lock:
                    self.results.append(result)
                if self.result_callback:
                    self.result_callback(result)
            finally:
                self.job_queue.task_done()

    def _create_media_for_job(self, job: BananaJob, lane: BananaLane, media_ids: list[str], end_media_id: Optional[str]) -> dict:
        self._delay_before_create_submit(job, lane)
        runtime = lane.runtime
        client = runtime.client
        if job.mode == 'video':
            with runtime.video_create_lock:
                self._wait_for_rate_limit_cooldown()
                logging.info('lane=%s job=%s stage=video-submit-start', lane.lane_id, job.job_id)
                if job.video_type == 'text':
                    created = client.create_video_from_text(
                        access_token=lane.access_token,
                        prompt=job.prompt,
                        aspect_ratio=job.aspect_ratio,
                        project_id=lane.project_id,
                    )
                elif job.video_type == 'reference':
                    created = client.create_video_from_reference_image(
                        access_token=lane.access_token,
                        prompt=job.prompt,
                        aspect_ratio=job.aspect_ratio,
                        media_id=media_ids[0],
                        project_id=lane.project_id,
                    )
                elif end_media_id:
                    created = client.create_video_from_start_end_images(
                        access_token=lane.access_token,
                        prompt=job.prompt,
                        aspect_ratio=job.aspect_ratio,
                        start_media_id=media_ids[0],
                        end_media_id=end_media_id,
                        project_id=lane.project_id,
                    )
                else:
                    created = client.create_video_from_image(
                        access_token=lane.access_token,
                        prompt=job.prompt,
                        aspect_ratio=job.aspect_ratio,
                        media_id=media_ids[0],
                        project_id=lane.project_id,
                    )
                logging.info('lane=%s job=%s stage=video-submit-ok', lane.lane_id, job.job_id)
                return created

        logging.info('lane=%s job=%s stage=image-submit-start', lane.lane_id, job.job_id)
        previous_client_lane_id = client.lane_id
        try:
            client.lane_id = lane.lane_id
            created = client.create_image(
                access_token=lane.access_token,
                prompt=job.prompt,
                aspect_ratio=job.aspect_ratio,
                media_ids=media_ids,
                model=job.model,
                project_id=lane.project_id,
            )
        finally:
            client.lane_id = previous_client_lane_id
        logging.info('lane=%s job=%s stage=image-submit-ok', lane.lane_id, job.job_id)
        return created

    def _run_job_on_lane(self, job: BananaJob, lane: BananaLane) -> BananaJobResult:
        started_at = time.time()
        runtime = lane.runtime
        client = runtime.client
        while job.attempts < self.max_attempts:
            job.attempts += 1
            logging.info(
                'lane=%s job=%s token=%s attempt=%s stage=create',
                lane.lane_id,
                job.job_id,
                lane.token_short,
                job.attempts,
            )
            try:
                self._wait_for_rate_limit_cooldown()
                self._delay_before_request('attempt-job')
                self._warmup_runtime(runtime, lane.lane_id)
                media_ids = []
                image_reference_paths = job.reference_paths if job.reference_paths is not None else ([job.reference_path] if job.reference_path else [])
                for reference_path in image_reference_paths:
                    logging.info(
                        'lane=%s job=%s token=%s attempt=%s stage=upload reference=%s',
                        lane.lane_id,
                        job.job_id,
                        lane.token_short,
                        job.attempts,
                        reference_path,
                    )
                    media_ids.append(client.upload_reference(lane.access_token, reference_path, project_id=lane.project_id))

                end_media_id = None
                if job.end_reference_path:
                    logging.info(
                        'lane=%s job=%s token=%s attempt=%s stage=upload-end reference=%s',
                        lane.lane_id,
                        job.job_id,
                        lane.token_short,
                        job.attempts,
                        job.end_reference_path,
                    )
                    end_media_id = client.upload_reference(lane.access_token, job.end_reference_path, project_id=lane.project_id)

                if job.mode == 'video' and job.video_type != 'text' and not media_ids:
                    raise BananaClientError('Video mode requires one reference image')
                created = self._create_media_for_job(job, lane, media_ids, end_media_id)

                if created['type'] == 'sync':
                    result = created
                else:
                    operation_name = created['operationName']
                    scene_id = created['sceneId']
                    logging.info(
                        'lane=%s job=%s token=%s attempt=%s stage=poll operation=%s',
                        lane.lane_id,
                        job.job_id,
                        lane.token_short,
                        job.attempts,
                        operation_name,
                    )
                    result = self._poll_operation_with_token_refresh(
                        lane=lane,
                        operation_name=operation_name,
                        scene_id=scene_id,
                        failure_message='Media generation failed during async polling',
                        mode=job.mode,
                    )
                    if result is None:
                        raise BananaClientError('Polling timeout: media generation did not finish in time')

                if job.mode == 'image' and job.image_resolution == '4K':
                    raw_result = result.get('raw') or {}
                    media_id = client.extract_media_id(raw_result)
                    if not media_id:
                        raise BananaClientError('No mediaId found for 4K upsample')
                    upsampled = client.upsample_image_4k(
                        access_token=lane.access_token,
                        media_id=media_id,
                        project_id=lane.project_id,
                    )
                    if upsampled['type'] == 'sync':
                        result = upsampled
                    else:
                        operation_name = upsampled['operationName']
                        scene_id = upsampled['sceneId']
                        result = self._poll_operation_with_token_refresh(
                            lane=lane,
                            operation_name=operation_name,
                            scene_id=scene_id,
                            failure_message='4K upsample failed during async polling',
                            include_link=True,
                        )
                        if result is None:
                            raise BananaClientError('Polling timeout: 4K upsample did not finish in time')

                if job.mode == 'video':
                    original_result = result
                    original_download_url = result.get('downloadUrl')
                    raw_result = result.get('raw') or {}
                    media_id = client.extract_media_id(raw_result)
                    if not media_id:
                        logging.warning(
                            'lane=%s job=%s token=%s attempt=%s stage=video-upscale-1080-skip reason=no_media_id action=fallback_720p',
                            lane.lane_id,
                            job.job_id,
                            lane.token_short,
                            job.attempts,
                        )
                    else:
                        try:
                            logging.info(
                                'lane=%s job=%s token=%s attempt=%s stage=video-upscale-1080-submit media_id=%s',
                                lane.lane_id,
                                job.job_id,
                                lane.token_short,
                                job.attempts,
                                media_id,
                            )
                            upscaled = client.upscale_video_1080p(
                                access_token=lane.access_token,
                                media_id=media_id,
                                aspect_ratio=job.aspect_ratio,
                                project_id=lane.project_id,
                            )
                            if upscaled['type'] == 'sync':
                                result = upscaled
                                logging.info(
                                    'lane=%s job=%s token=%s attempt=%s stage=video-upscale-1080-ok mode=sync',
                                    lane.lane_id,
                                    job.job_id,
                                    lane.token_short,
                                    job.attempts,
                                )
                            else:
                                logging.info(
                                    'lane=%s job=%s token=%s attempt=%s stage=video-upscale-1080-poll operation=%s',
                                    lane.lane_id,
                                    job.job_id,
                                    lane.token_short,
                                    job.attempts,
                                    upscaled['operationName'],
                                )
                                upscaled_result = self._poll_operation_with_token_refresh(
                                    lane=lane,
                                    operation_name=upscaled['operationName'],
                                    scene_id=upscaled['sceneId'],
                                    failure_message='1080p video upscale failed during async polling',
                                )
                                if upscaled_result and upscaled_result.get('downloadUrl'):
                                    result = upscaled_result
                                    logging.info(
                                        'lane=%s job=%s token=%s attempt=%s stage=video-upscale-1080-ok mode=async',
                                        lane.lane_id,
                                        job.job_id,
                                        lane.token_short,
                                        job.attempts,
                                    )
                                else:
                                    result = original_result
                                    logging.warning(
                                        'lane=%s job=%s token=%s attempt=%s stage=video-upscale-1080-timeout action=fallback_720p',
                                        lane.lane_id,
                                        job.job_id,
                                        lane.token_short,
                                        job.attempts,
                                    )
                        except Exception as upscale_exc:
                            result = original_result
                            logging.warning(
                                'lane=%s job=%s token=%s attempt=%s stage=video-upscale-1080-failed action=fallback_720p error=%s',
                                lane.lane_id,
                                job.job_id,
                                lane.token_short,
                                job.attempts,
                                upscale_exc,
                            )
                    if not result.get('downloadUrl') and original_download_url:
                        result = dict(result)
                        result['downloadUrl'] = original_download_url

                with runtime.reset_lock:
                    runtime.consecutive_403 = 0
                download_url = result.get('downloadUrl')
                saved_path = None
                if download_url and job.output_path:
                    saved_path = client.save_remote_file(download_url, job.output_path)
                duration = round(time.time() - started_at, 2)
                return BananaJobResult(
                    job_id=job.job_id,
                    status='completed',
                    prompt=job.prompt,
                    lane_id=lane.lane_id,
                    token_fingerprint=lane.token_short,
                    attempts=job.attempts,
                    download_url=download_url,
                    saved_path=saved_path,
                    duration_seconds=duration,
                )
            except Exception as exc:
                message = str(exc)
                logging.warning(
                    'lane=%s job=%s token=%s attempt=%s stage=failure error=%s',
                    lane.lane_id,
                    job.job_id,
                    lane.token_short,
                    job.attempts,
                    message,
                )
                if self._is_401_error(message):
                    runtime.invalid_auth = True
                    logging.warning(
                        'lane=%s token=%s stage=token_invalid reason=401 action=disable_token',
                        lane.lane_id,
                        lane.token_short,
                    )
                    return self._run_job_on_lane(job, self._get_next_available_lane(lane)) if self._get_next_available_lane(lane) else BananaJobResult(
                        job_id=job.job_id,
                        status='failed',
                        prompt=job.prompt,
                        lane_id=lane.lane_id,
                        token_fingerprint=lane.token_short,
                        attempts=job.attempts,
                        error=message,
                        duration_seconds=round(time.time() - started_at, 2),
                    )
                if self._is_429_error(message):
                    self._apply_429_cooldown(message)
                self._handle_failure(lane, message)

                if self._is_500_error(message) and job.attempts < self.max_attempts:
                    rotated_proxy = self._rotate_runtime_proxy_for_500(job, lane, message)
                    if not rotated_proxy:
                        self._apply_500_backoff(job, lane, message)

                if job.attempts >= self.max_attempts:
                    return BananaJobResult(
                        job_id=job.job_id,
                        status='failed',
                        prompt=job.prompt,
                        lane_id=lane.lane_id,
                        token_fingerprint=lane.token_short,
                        attempts=job.attempts,
                        error=message,
                        duration_seconds=round(time.time() - started_at, 2),
                    )
        return BananaJobResult(
            job_id=job.job_id,
            status='failed',
            prompt=job.prompt,
            lane_id=lane.lane_id,
            token_fingerprint=lane.token_short,
            attempts=job.attempts,
            error='Job failed',
            duration_seconds=round(time.time() - started_at, 2),
        )

