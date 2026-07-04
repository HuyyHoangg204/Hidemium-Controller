import base64
import cgi
import json
import logging
import mimetypes
import os
import secrets
import string
import tempfile
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

from scheduler import BananaJob, BananaScheduler


@dataclass
class ApiServerConfig:
    tokens_provider: Callable[[], list[str]]
    proxies_provider: Callable[[], list[str]]
    token_project_map_provider: Callable[[], dict[str, str]]
    save_dir_provider: Callable[[], str]
    thread_count_provider: Callable[[], int]
    runtime_root: Path
    log_callback: Optional[Callable[[str], None]] = None


class LocalApiServer:
    def __init__(self, config: ApiServerConfig, host: str = '127.0.0.1', port: int = 3000):
        self.config = config
        self.host = host
        self.port = int(port)
        self.api_key = (os.environ.get('LOCAL_API_KEY') or self._random_api_key()).strip()
        self.tasks: dict[str, dict] = {}
        self.tasks_lock = threading.RLock()
        self.httpd: ThreadingHTTPServer | None = None
        self.server_thread: threading.Thread | None = None
        self.running_schedulers = set()
        self.schedulers_lock = threading.RLock()

    def start(self) -> None:
        if self.httpd:
            return
        handler = self._make_handler()
        self.httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self.server_thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.server_thread.start()
        self._log(f'API server started at http://localhost:{self.port}/')
        self._log(f'LOCAL_API_KEY={self.api_key}')

    def stop(self) -> None:
        httpd = self.httpd
        self.httpd = None
        if httpd:
            httpd.shutdown()
            httpd.server_close()
        with self.schedulers_lock:
            schedulers = list(self.running_schedulers)
        for scheduler in schedulers:
            try:
                scheduler.shutdown()
            except Exception as exc:
                self._log(f'API scheduler shutdown failed: {exc}')
        self._log('API server stopped')

    def is_running(self) -> bool:
        return self.httpd is not None

    def _log(self, message: str) -> None:
        logging.info(message)
        if self.config.log_callback:
            try:
                self.config.log_callback(message)
            except Exception:
                pass

    def _random_api_key(self) -> str:
        alphabet = string.ascii_letters + string.digits
        return ''.join(secrets.choice(alphabet) for _ in range(12))

    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                server._log('API ' + (fmt % args))

            def do_OPTIONS(self):
                self._send_json({'success': True})

            def do_GET(self):
                parsed = urlparse(self.path)
                path = parsed.path.rstrip('/') or '/'
                query = parse_qs(parsed.query)
                if path in {'/', '/health', '/api/health'}:
                    self._send_json({'success': True, 'data': {'status': 'OK', 'server': 'local-api', 'port': server.port}})
                    return
                if not self._authorized():
                    self._send_error('Unauthorized', 'UNAUTHORIZED', 401)
                    return
                if path in {'/tasks', '/api/veo/videos'}:
                    with server.tasks_lock:
                        data = list(server.tasks.values())
                    self._send_json({'success': True, 'data': data})
                    return
                if path == '/api/veo/video':
                    task_id = (query.get('task_id') or query.get('id') or [''])[0]
                    self._send_task(task_id)
                    return
                if path.startswith('/tasks/'):
                    task_id = path.split('/', 2)[2]
                    self._send_task(task_id)
                    return
                self._send_error('Not found', 'NOT_FOUND', 404)

            def do_POST(self):
                parsed = urlparse(self.path)
                path = parsed.path.rstrip('/') or '/'
                if not self._authorized():
                    self._send_error('Unauthorized', 'UNAUTHORIZED', 401)
                    return
                try:
                    if path == '/tasks':
                        payload = self._read_json()
                        task = server.create_task(payload)
                    elif path in {'/api/veo/text-to-image', '/api/veo/create-image'}:
                        payload = self._read_request_payload()
                        task = server.create_public_task(payload, 'CREATE_IMAGE')
                    elif path in {'/api/veo/image-to-video', '/api/veo/image-to-video-json'}:
                        payload = self._read_request_payload()
                        task = server.create_public_task(payload, 'IMAGE_TO_VIDEO')
                    elif path in {'/api/veo/frames-to-video', '/api/veo/frames-to-video-json'}:
                        payload = self._read_request_payload()
                        task = server.create_public_task(payload, 'FRAMES_TO_VIDEO')
                    else:
                        self._send_error('Not found', 'NOT_FOUND', 404)
                        return
                    self._send_json({'success': True, 'data': task})
                except Exception as exc:
                    self._send_error(str(exc), 'BAD_REQUEST', 400)

            def _authorized(self) -> bool:
                expected = server.api_key
                if not expected:
                    return True
                auth = self.headers.get('Authorization') or ''
                x_key = self.headers.get('X-API-Key') or ''
                if auth.startswith('Bearer '):
                    return auth.split(' ', 1)[1].strip() == expected
                return x_key.strip() == expected

            def _send_task(self, task_id: str):
                with server.tasks_lock:
                    task = server.tasks.get(task_id)
                if not task:
                    self._send_error('Task not found', 'TASK_NOT_FOUND', 404)
                    return
                self._send_json({'success': True, 'data': task})

            def _read_request_payload(self) -> dict:
                content_type = self.headers.get('Content-Type') or ''
                if content_type.lower().startswith('multipart/form-data'):
                    return self._read_multipart()
                return self._read_json()

            def _read_json(self) -> dict:
                length = int(self.headers.get('Content-Length') or 0)
                raw = self.rfile.read(length) if length else b'{}'
                return json.loads(raw.decode('utf-8') or '{}')

            def _read_multipart(self) -> dict:
                form = cgi.FieldStorage(
                    fp=self.rfile,
                    headers=self.headers,
                    environ={
                        'REQUEST_METHOD': 'POST',
                        'CONTENT_TYPE': self.headers.get('Content-Type') or '',
                    },
                    keep_blank_values=True,
                )
                payload: dict = {}
                files = []
                for key in form.keys():
                    values = form[key]
                    items = values if isinstance(values, list) else [values]
                    for item in items:
                        if item.filename:
                            files.append(server._save_uploaded_file(item.file.read(), item.filename, item.type))
                            continue
                        value = item.value
                        clean_key = key[:-2] if key.endswith('[]') else key
                        if clean_key in {'prompts', 'image_paths'}:
                            payload.setdefault(clean_key, []).append(value)
                        elif clean_key in payload:
                            if not isinstance(payload[clean_key], list):
                                payload[clean_key] = [payload[clean_key]]
                            payload[clean_key].append(value)
                        else:
                            payload[clean_key] = value
                if files:
                    payload['image_paths'] = files
                return payload

            def _send_error(self, error: str, code: str = 'ERROR', status: int = 400):
                self._send_json({'success': False, 'error': error, 'code': code}, status=status)

            def _send_json(self, data: dict, status: int = 200):
                body = json.dumps(data, ensure_ascii=False).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-API-Key')
                self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler

    def create_public_task(self, payload: dict, action_type: str) -> dict:
        prompt = payload.get('prompt')
        prompts = payload.get('prompts') or ([prompt] if prompt else [])
        public_payload = {
            'action_type': action_type,
            'project_id': payload.get('project_id'),
            'name': payload.get('name'),
            'model': payload.get('model') or ('NARWHAL' if action_type == 'CREATE_IMAGE' else 'FAST'),
            'screen_ratio': payload.get('screen_ratio') or payload.get('aspect') or payload.get('aspect_ratio') or '16:9',
            'prompts': prompts,
            'upsample_resolution': payload.get('upsample_resolution'),
            'save_dir': payload.get('save_dir'),
            'source': payload.get('source') or 'public-api',
            'thread_count': payload.get('thread_count'),
        }
        image_paths = self._collect_image_inputs(payload)
        if image_paths:
            public_payload['image_paths'] = image_paths
        return self.create_task(public_payload)

    def create_task(self, payload: dict) -> dict:
        action_type = str(payload.get('action_type') or '').strip().upper()
        if action_type not in {'CREATE_IMAGE', 'IMAGE_TO_VIDEO', 'FRAMES_TO_VIDEO'}:
            raise ValueError('action_type must be CREATE_IMAGE, IMAGE_TO_VIDEO, or FRAMES_TO_VIDEO')

        prompts = payload.get('prompts') or []
        if isinstance(prompts, str):
            prompts = [prompts]
        prompts = [str(p).strip() for p in prompts if str(p).strip()]
        if not prompts:
            raise ValueError('prompts is required')

        image_paths = payload.get('image_paths')
        if image_paths is None:
            image_paths = []
        if isinstance(image_paths, str):
            image_paths = [image_paths]
        image_paths = [self._normalize_path(path) for path in image_paths if str(path).strip()]

        if action_type == 'IMAGE_TO_VIDEO' and len(image_paths) < 1:
            raise ValueError('IMAGE_TO_VIDEO requires image_paths[0]')
        if action_type == 'FRAMES_TO_VIDEO' and len(image_paths) < 2:
            raise ValueError('FRAMES_TO_VIDEO requires image_paths[0] and image_paths[1]')

        upsample_resolution = payload.get('upsample_resolution')
        if upsample_resolution in ('', None):
            upsample_resolution = None
        if upsample_resolution not in {None, '2K', '4K'}:
            raise ValueError('upsample_resolution must be null, 2K, or 4K')
        if upsample_resolution == '2K':
            raise ValueError('2K upscale is not supported by current client yet')

        now = time.time()
        task_id = str(uuid.uuid4())
        project_id = str(payload.get('project_id') or uuid.uuid4())
        model = str(payload.get('model') or ('NARWHAL' if action_type == 'CREATE_IMAGE' else 'FAST'))
        screen_ratio = str(payload.get('screen_ratio') or payload.get('aspect_ratio') or '16:9')
        name = str(payload.get('name') or self._default_name(action_type, model, prompts[0]))
        task = {
            'id': task_id,
            'project_id': project_id,
            'name': name,
            'action_type': action_type,
            'model': model,
            'screen_ratio': screen_ratio,
            'prompts': prompts,
            'status': 'PENDING',
            'created_at': now,
            'updated_at': now,
            'media_id': None,
            'output_filename': None,
            'error': None,
            'image_refs': None,
            'completed_at': None,
            'picked_account_name': None,
            'source': payload.get('source'),
            'image_paths': image_paths or None,
        }
        with self.tasks_lock:
            self.tasks[task_id] = task
        threading.Thread(target=self._run_task, args=(task_id, payload, task), daemon=True).start()
        return dict(task)

    def _run_task(self, task_id: str, payload: dict, task: dict) -> None:
        scheduler = None
        try:
            tokens = self.config.tokens_provider()
            if not tokens:
                raise ValueError('No saved tokens available')
            save_dir = Path(str(payload.get('save_dir') or self.config.save_dir_provider() or 'outputs')) / 'api' / task_id
            save_dir.mkdir(parents=True, exist_ok=True)
            prompt = task['prompts'][0]
            extension = 'jpg' if task['action_type'] == 'CREATE_IMAGE' else 'mp4'
            output_path = save_dir / f'{task_id}.{extension}'
            video_type = 'single'
            reference_path = None
            end_reference_path = None
            mode = 'image'
            if task['action_type'] == 'IMAGE_TO_VIDEO':
                mode = 'video'
                video_type = 'single'
                reference_path = task['image_paths'][0]
            elif task['action_type'] == 'FRAMES_TO_VIDEO':
                mode = 'video'
                video_type = 'start_end'
                reference_path = task['image_paths'][0]
                end_reference_path = task['image_paths'][1]

            job = BananaJob(
                mode=mode,
                prompt=prompt,
                model=task['model'],
                aspect_ratio=task['screen_ratio'],
                reference_path=reference_path,
                output_path=str(output_path),
                video_type=video_type,
                end_reference_path=end_reference_path,
                image_resolution='4K' if payload.get('upsample_resolution') == '4K' else '1K',
            )
            scheduler = BananaScheduler(
                tokens=tokens,
                thread_count=max(1, int(payload.get('thread_count') or self.config.thread_count_provider() or 1)),
                max_attempts=5,
                runtime_dir=str(self.config.runtime_root / 'api' / task_id),
                token_project_map=self.config.token_project_map_provider(),
                proxies=self.config.proxies_provider(),
            )
            with self.schedulers_lock:
                self.running_schedulers.add(scheduler)
            results = scheduler.submit([job])
            result = results[0] if results else None
            if not result or result.status != 'completed':
                raise RuntimeError((result.error if result else None) or 'Task failed')
            self._update_task(task_id, status='COMPLETED', output_filename=result.saved_path, media_id=result.download_url, completed_at=time.time(), updated_at=time.time(), error=None)
        except Exception as exc:
            self._update_task(task_id, status='FAILED', error=str(exc), completed_at=time.time(), updated_at=time.time())
        finally:
            if scheduler:
                try:
                    scheduler.shutdown()
                except Exception:
                    pass
                with self.schedulers_lock:
                    self.running_schedulers.discard(scheduler)

    def _collect_image_inputs(self, payload: dict) -> list[str]:
        image_paths = payload.get('image_paths') or payload.get('images') or []
        if isinstance(image_paths, (str, dict)):
            image_paths = [image_paths]
        collected = []
        for item in image_paths:
            if isinstance(item, dict):
                if item.get('url'):
                    collected.append(self._download_image_url(item['url'], item.get('name')))
                elif item.get('path'):
                    collected.append(str(item['path']))
                elif item.get('b64'):
                    collected.append(self._save_base64_image(item.get('b64'), item.get('name'), item.get('mime')))
            else:
                text = str(item).strip()
                if text.startswith(('http://', 'https://')):
                    collected.append(self._download_image_url(text, None))
                elif text:
                    collected.append(text)
        for item in payload.get('images_b64') or payload.get('image_refs_b64') or []:
            if isinstance(item, dict):
                collected.append(self._save_base64_image(item.get('b64'), item.get('name'), item.get('mime')))
            elif isinstance(item, str):
                collected.append(self._save_base64_image(item, None, None))
        return collected

    def _save_uploaded_file(self, content: bytes, filename: str, mime_type: Optional[str] = None) -> str:
        suffix = Path(filename or '').suffix or mimetypes.guess_extension(mime_type or '') or '.jpg'
        upload_dir = self.config.runtime_root / 'api_uploads'
        upload_dir.mkdir(parents=True, exist_ok=True)
        output = upload_dir / f'{uuid.uuid4().hex}{suffix}'
        output.write_bytes(content)
        return str(output)

    def _save_base64_image(self, b64_text: str, name: Optional[str] = None, mime_type: Optional[str] = None) -> str:
        if not b64_text:
            raise ValueError('Base64 image is empty')
        clean = str(b64_text).strip()
        if ',' in clean and clean.lower().startswith('data:'):
            header, clean = clean.split(',', 1)
            if not mime_type and ';' in header:
                mime_type = header.split(':', 1)[1].split(';', 1)[0]
        suffix = Path(name or '').suffix or mimetypes.guess_extension(mime_type or 'image/jpeg') or '.jpg'
        upload_dir = self.config.runtime_root / 'api_uploads'
        upload_dir.mkdir(parents=True, exist_ok=True)
        output = upload_dir / f'{uuid.uuid4().hex}{suffix}'
        output.write_bytes(base64.b64decode(clean))
        return str(output)

    def _download_image_url(self, url: str, name: Optional[str] = None) -> str:
        suffix = Path(name or urlparse(url).path).suffix or '.jpg'
        upload_dir = self.config.runtime_root / 'api_uploads'
        upload_dir.mkdir(parents=True, exist_ok=True)
        output = upload_dir / f'{uuid.uuid4().hex}{suffix}'
        with urllib.request.urlopen(url, timeout=60) as response:
            output.write_bytes(response.read())
        return str(output)

    def _update_task(self, task_id: str, **updates) -> None:
        with self.tasks_lock:
            task = self.tasks.get(task_id)
            if task:
                task.update(updates)

    def _normalize_path(self, raw_path) -> str:
        path_text = str(raw_path or '').strip().strip('"').strip("'")
        if not path_text:
            return ''
        path_text = path_text.replace('\\', '/')
        app_dir = Path(__file__).resolve().parent
        relative_text = path_text.lstrip('/')
        search_roots = [
            app_dir,
            app_dir.parent,
            Path.cwd(),
            Path.cwd().parent,
        ]
        app_prefixed = f'{app_dir.name}/'
        if relative_text.startswith(app_prefixed):
            relative_candidates = [relative_text, relative_text[len(app_prefixed):]]
        else:
            relative_candidates = [relative_text]
        for root in search_roots:
            for relative_candidate in relative_candidates:
                candidate = root / relative_candidate
                if candidate.exists():
                    return str(candidate)

        candidate = Path(path_text)
        if candidate.is_absolute() and not path_text.startswith('/'):
            return str(candidate)
        fallback_relative = relative_candidates[-1]
        return str(app_dir / fallback_relative)

    def _default_name(self, action_type: str, model: str, prompt: str) -> str:
        if action_type == 'CREATE_IMAGE':
            return f'{model}: {prompt[:36]}'
        if action_type == 'FRAMES_TO_VIDEO':
            return 'video name'
        return 'video from image'
