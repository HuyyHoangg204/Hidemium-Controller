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

from .banana_client import BananaImageClient, BananaClientError


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
            runtime = BananaTokenRuntime(
                token=token,
                proxy=proxy,
                project_id=self.token_project_map.get(token),
                client=BananaImageClient(
                    logger=lambda message: logging.info(message),
                    lane_id=f'token-{index + 1}-{token_short}',
                    user_data_dir=str(profile_dir),
                    proxy_server=proxy,
                ),
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
        if REQUEST_DELAY_SECONDS > 0:
            time.sleep(REQUEST_DELAY_SECONDS)

    def _delay_before_create_submit(self, job: BananaJob, lane: BananaLane) -> None:
        if job.mode == 'image':
            delay = random.uniform(IMAGE_RANDOM_DELAY_MIN_SECONDS, IMAGE_RANDOM_DELAY_MAX_SECONDS)
            logging.info('lane=%s job=%s stage=image-submit-delay seconds=%.2f', lane.lane_id, job.job_id, delay)
            time.sleep(delay)
        else:
            logging.info('lane=%s job=%s stage=video-submit-wait waiting_for_previous_create=1', lane.lane_id, job.job_id)

    def submit(self, jobs: list[BananaJob]) -> list[BananaJobResult]:
        if not jobs:
            return []

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

        return list(self.results)

    def shutdown(self) -> None:
        for runtime in self.runtimes:
            try:
                runtime.client.shutdown(remove_profile=False)
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
                runtime.client.clear_browser_data()
                runtime.ready_event.set()
            runtime.last_reset_at = time.time()
            runtime.consecutive_403 = 0

    def _warmup_runtime(self, runtime: BananaTokenRuntime, lane_id: str) -> None:
        if runtime.ready_event.is_set():
            return
        with runtime.warmup_lock:
            if runtime.ready_event.is_set():
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
            logging.info('lane=%s token=%s stage=warmup action=ready', lane_id, runtime.token_short)

    def _wait_for_rate_limit_cooldown(self) -> None:
        while True:
            with self.rate_limit_lock:
                wait_seconds = self.rate_limit_until - time.time()
            if wait_seconds <= 0:
                return
            logging.warning('stage=429 action=wait cooldown_remaining=%.2fs', wait_seconds)
            time.sleep(min(wait_seconds, 1.0))

    def _apply_429_cooldown(self, message: str) -> None:
        until = time.time() + RATE_LIMIT_429_COOLDOWN_SECONDS
        with self.rate_limit_lock:
            self.rate_limit_until = max(self.rate_limit_until, until)
        logging.warning('stage=429 action=cooldown seconds=%.2f error=%s', RATE_LIMIT_429_COOLDOWN_SECONDS, message)

    def _is_429_error(self, message: str) -> bool:
        lowered = (message or '').lower()
        return '429' in lowered or 'too many requests' in lowered or 'resource_exhausted' in lowered

    def _is_500_error(self, message: str) -> bool:
        lowered = (message or '').lower()
        return 'http 500' in lowered or 'internal error encountered' in lowered or 'internal server error' in lowered

    def _apply_500_backoff(self, job: BananaJob, lane: BananaLane, message: str) -> None:
        delay = min(SERVER_500_BACKOFF_MAX_SECONDS, SERVER_500_BACKOFF_BASE_SECONDS * max(1, job.attempts))
        jitter = random.uniform(0.0, 5.0)
        wait_seconds = delay + jitter
        logging.warning(
            'lane=%s job=%s token=%s attempt=%s stage=server_500_backoff action=retry_same_token seconds=%.2f error=%s',
            lane.lane_id,
            job.job_id,
            lane.token_short,
            job.attempts,
            wait_seconds,
            message,
        )
        time.sleep(wait_seconds)

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
                'Flow page navigation failed',
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
        created = client.create_image(
            access_token=lane.access_token,
            prompt=job.prompt,
            aspect_ratio=job.aspect_ratio,
            media_ids=media_ids,
            model=job.model,
            project_id=lane.project_id,
        )
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
                    result = None
                    for poll_attempt in range(1, 31):
                        time.sleep(7.0)
                        polled = client.poll_image(lane.access_token, operation_name, scene_id)
                        if polled['done'] and not polled['failed'] and polled.get('downloadUrl'):
                            result = {'type': 'sync', 'downloadUrl': polled['downloadUrl'], 'raw': polled.get('raw')}
                            break
                        if polled['done'] and polled['failed']:
                            raise BananaClientError('Media generation failed during async polling')
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
                        result = None
                        for poll_attempt in range(1, 31):
                            time.sleep(7.0)
                            polled = client.poll_image(lane.access_token, operation_name, scene_id)
                            if polled['done'] and not polled['failed'] and polled.get('downloadUrl'):
                                result = {'type': 'sync', 'downloadUrl': polled['downloadUrl'], 'link': polled['downloadUrl'], 'raw': polled.get('raw')}
                                break
                            if polled['done'] and polled['failed']:
                                raise BananaClientError('4K upsample failed during async polling')
                        if result is None:
                            raise BananaClientError('Polling timeout: 4K upsample did not finish in time')

                with runtime.reset_lock:
                    runtime.consecutive_403 = 0
                download_url = result.get('downloadUrl')
                saved_path = None
                if download_url and job.output_path:
                    saved_path = client.save_remote_file(download_url, job.output_path)
                duration = round(time.time() - started_at, 2)
                logging.info(
                    'lane=%s job=%s token=%s attempt=%s stage=completed duration=%.2fs download_url=%s saved=%s',
                    lane.lane_id,
                    job.job_id,
                    lane.token_short,
                    job.attempts,
                    duration,
                    download_url,
                    saved_path,
                )
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


