import asyncio
import base64
import json
import logging
import mimetypes
import os
import random
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx
import requests
from playwright.async_api import Error as PlaywrightError, async_playwright


def _build_http_client() -> httpx.Client:
    """Create an httpx.Client that avoids urllib3 SSL EOF errors on large uploads."""
    transport = httpx.HTTPTransport(
        retries=3,
        http2=False,
    )
    return httpx.Client(
        transport=transport,
        timeout=httpx.Timeout(connect=15.0, read=120.0, write=120.0, pool=30.0),
        follow_redirects=True,
    )

from .browser_scripts import EXECUTE_IMAGE_JS, RECAPTCHA_READY_JS


SITE_KEY = '6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV'
FLOW_URL = 'https://labs.google/fx/tools/flow'
PROJECT_ID = '14db602e-138c-47de-8141-4b0c0f3346f1'
IMAGE_CREATE_URL_TEMPLATE = 'https://aisandbox-pa.googleapis.com/v1/projects/{project_id}/flowMedia:batchGenerateImages'
IMAGE_CREATE_URL = IMAGE_CREATE_URL_TEMPLATE.format(project_id=PROJECT_ID)
VIDEO_CREATE_URL = 'https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoStartImage'
VIDEO_TEXT_CREATE_URL = 'https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoText'
VIDEO_START_END_CREATE_URL = 'https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoStartAndEndImage'
VIDEO_REFERENCE_IMAGES_CREATE_URL = 'https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoReferenceImages'
VIDEO_TEXT_CREATE_URL = 'https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoText'
FLOW_UPLOAD_URL = 'https://aisandbox-pa.googleapis.com/v1/flow/uploadImage'
UPSAMPLE_IMAGE_URL = 'https://aisandbox-pa.googleapis.com/v1/flow/upsampleImage'
POLL_URL = 'https://aisandbox-pa.googleapis.com/v1/video:batchCheckAsyncVideoGenerationStatus'
HIDDEN_LEFT = -32000
HIDDEN_TOP = -32000
WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 720


class BananaClientError(Exception):
    pass


def normalize_chrome_proxy(proxy_value: Optional[str]) -> Optional[str]:
    proxy_value = (proxy_value or '').strip()
    if not proxy_value:
        return None
    if '://' not in proxy_value:
        return f'http://{proxy_value}'
    return proxy_value


def redact_proxy_for_log(proxy_value: Optional[str]) -> str:
    normalized = normalize_chrome_proxy(proxy_value)
    if not normalized:
        return ''
    try:
        parts = urlsplit(normalized)
    except Exception:
        return normalized
    netloc = parts.netloc or ''
    if '@' in netloc:
        credentials, host_part = netloc.rsplit('@', 1)
        if ':' in credentials:
            username = credentials.split(':', 1)[0]
            netloc = f'{username}:***@{host_part}'
        else:
            netloc = f'***@{host_part}'
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def split_proxy_components(proxy_value: Optional[str]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    normalized = normalize_chrome_proxy(proxy_value)
    if not normalized:
        return None, None, None
    parts = urlsplit(normalized)
    host = parts.hostname
    if not host:
        raise BananaClientError(f'Invalid proxy value: {proxy_value}')
    scheme = parts.scheme or 'http'
    server = f'{scheme}://{host}'
    if parts.port:
        server = f'{server}:{parts.port}'
    username = parts.username or None
    password = parts.password or None
    return server, username, password


def get_video_model_key(aspect_ratio: str, tier: str = 'PAYGATE_TIER_TWO', model_speed: str = 'relaxed') -> str:
    is_portrait = aspect_ratio == '9:16'
    ultra = {
        'i2v': 'veo_3_1_i2v_s_fast_portrait_ultra' if is_portrait else 'veo_3_1_i2v_s_fast_ultra',
    }
    standard = {
        'i2v': 'veo_3_1_i2v_s_fast_portrait' if is_portrait else 'veo_3_1_i2v_s_fast',
    }
    if tier == 'PAYGATE_TIER_TWO':
        return ultra['i2v']
    return standard['i2v']


def get_reference_video_model_key(aspect_ratio: str, tier: str = 'PAYGATE_TIER_TWO', model_speed: str = 'relaxed') -> str:
    is_portrait = aspect_ratio == '9:16'
    if tier == 'PAYGATE_TIER_TWO':
        return 'veo_3_1_r2v_fast_portrait_ultra' if is_portrait else 'veo_3_1_r2v_fast_landscape_ultra'
    return 'veo_3_1_r2v_fast_portrait' if is_portrait else 'veo_3_1_r2v_fast'


def get_text_video_model_key(aspect_ratio: str, tier: str = 'PAYGATE_TIER_TWO', model_speed: str = 'relaxed') -> str:
    is_portrait = aspect_ratio == '9:16'
    if tier == 'PAYGATE_TIER_TWO':
        return 'veo_3_1_t2v_fast_ultra'
    return 'veo_3_1_t2v_fast_portrait' if is_portrait else 'veo_3_1_t2v_fast'


class FlowBrowser:
    def __init__(self, logger: Callable[[str], None], lane_id: str = 'shared', user_data_dir: Optional[str] = None, proxy_server: Optional[str] = None):
        self.logger = logger
        self.lane_id = lane_id
        self.proxy_server = normalize_chrome_proxy(proxy_server)
        self._proxy_server, self._proxy_username, self._proxy_password = split_proxy_components(self.proxy_server)
        self._pw = None
        self._browser_context = None
        self._browser = None
        self._page = None
        self._chrome_proc = None
        self._cdp_session = None
        self._debug_port = None
        self._loop = None
        self._thread = None
        self._thread_start_lock = threading.Lock()
        self._thread_ready = threading.Event()
        self._ensure_lock = None
        default_dir = Path(tempfile.gettempdir()) / f'banana_doc_python_app_profile_{lane_id}'
        self._user_data_dir = Path(user_data_dir) if user_data_dir else default_dir
        self._proxy_extension_dir = self._user_data_dir / 'proxy_auth_extension'

    def _log(self, message: str) -> None:
        if self.logger:
            self.logger(message)

    def _find_chrome(self) -> str:
        candidates = [
            r'C:\Program Files\Google\Chrome\Application\chrome.exe',
            r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
            str(Path.home() / r'AppData\Local\Google\Chrome\Application\chrome.exe'),
        ]
        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate
        raise BananaClientError('Chrome executable not found')

    def _is_flow_url(self, url: str) -> bool:
        return bool(url) and url.lower().startswith('https://labs.google/fx/') and '/tools/flow' in url.lower()

    def _is_browser_internal_url(self, url: str) -> bool:
        lowered = (url or '').lower()
        return (
            not lowered
            or lowered == 'about:blank'
            or lowered.startswith('chrome://')
            or lowered.startswith('chrome-search://')
            or lowered.startswith('devtools://')
        )

    def _wait_for_debug_endpoint(self, port: int, timeout: float = 45.0) -> str:
        deadline = time.time() + timeout
        url = f'http://127.0.0.1:{port}/json/version'
        last_error = None
        attempts = 0
        while time.time() < deadline:
            attempts += 1
            if self._chrome_proc and self._chrome_proc.poll() is not None:
                raise BananaClientError(
                    f'Chrome exited before debug port became ready '
                    f'(pid={self._chrome_proc.pid}, code={self._chrome_proc.returncode}, port={port})'
                )
            try:
                response = requests.get(url, timeout=2.0)
                response.raise_for_status()
                data = response.json()
                ws_url = data.get('webSocketDebuggerUrl')
                if ws_url:
                    self._log(f'[Browser:{self.lane_id}] Debug endpoint ready on port {port} after {attempts} checks')
                    return ws_url
            except Exception as exc:
                last_error = exc
            time.sleep(0.5)
        raise BananaClientError(f'Could not connect to Chrome debug port {port} after {timeout:.0f}s: {last_error}')

    async def _ensure_proxy_auth_async(self, page) -> None:
        if not (self._proxy_username or self._proxy_password):
            return
        if self._cdp_session is not None:
            return
        session = await self._browser_context.new_cdp_session(page)

        async def handle_auth_required(params):
            response = 'ProvideCredentials'
            if not self._proxy_username and not self._proxy_password:
                response = 'Default'
            try:
                await session.send('Fetch.continueWithAuth', {
                    'requestId': params['requestId'],
                    'authChallengeResponse': {
                        'response': response,
                        'username': self._proxy_username or '',
                        'password': self._proxy_password or '',
                    },
                })
            except PlaywrightError:
                pass

        async def handle_request_paused(params):
            try:
                await session.send('Fetch.continueRequest', {
                    'requestId': params['requestId'],
                })
            except PlaywrightError:
                pass

        session.on('Fetch.authRequired', lambda params: asyncio.create_task(handle_auth_required(params)))
        session.on('Fetch.requestPaused', lambda params: asyncio.create_task(handle_request_paused(params)))
        await session.send('Fetch.enable', {
            'handleAuthRequests': True,
            'patterns': [{'urlPattern': '*'}],
        })
        self._cdp_session = session
        self._log(f'[Browser:{self.lane_id}] Proxy auth CDP handler ready')

    def _prepare_proxy_auth_extension(self) -> Optional[Path]:
        if not (self._proxy_username or self._proxy_password):
            return None
        self._proxy_extension_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            'version': '1.0.0',
            'manifest_version': 2,
            'name': 'Antigravity Proxy Auth',
            'permissions': [
                'proxy',
                'tabs',
                'unlimitedStorage',
                'storage',
                '<all_urls>',
                'webRequest',
                'webRequestBlocking',
            ],
            'background': {'scripts': ['background.js']},
            'minimum_chrome_version': '22.0.0',
        }
        background = f"""
chrome.webRequest.onAuthRequired.addListener(
  function(details) {{
    return {{authCredentials: {{username: {json.dumps(self._proxy_username or '')}, password: {json.dumps(self._proxy_password or '')}}}}};
  }},
  {{urls: ['<all_urls>']}},
  ['blocking']
);
""".strip()
        (self._proxy_extension_dir / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        (self._proxy_extension_dir / 'background.js').write_text(background, encoding='utf-8')
        return self._proxy_extension_dir

    def _terminate_chrome_process(self) -> None:
        try:
            if self._chrome_proc and self._chrome_proc.poll() is None:
                self._chrome_proc.kill()
                self._chrome_proc.wait(timeout=5)
        except Exception:
            pass
        self._chrome_proc = None
        self._debug_port = None

    def _spawn_chrome_once(self, chrome_path: str) -> str:
        self._debug_port = random.randint(9222, 11222)
        args = [
            chrome_path,
            f'--remote-debugging-port={self._debug_port}',
            f'--user-data-dir={self._user_data_dir}',
            f'--window-position={HIDDEN_LEFT},{HIDDEN_TOP}',
            f'--window-size={WINDOW_WIDTH},{WINDOW_HEIGHT}',
            '--start-minimized',
            '--disable-blink-features=AutomationControlled',
            '--ignore-certificate-errors',
            '--allow-insecure-localhost',
            '--no-sandbox',
            '--no-first-run',
            '--no-default-browser-check',
            '--disable-background-mode',
            '--disable-features=Translate,OptimizationHints,MediaRouter',
            '--new-window',
            'about:blank',
        ]
        if self._proxy_server:
            args.insert(-1, f'--proxy-server={self._proxy_server}')
        proxy_extension_dir = self._prepare_proxy_auth_extension()
        if proxy_extension_dir:
            args.insert(-1, f'--disable-extensions-except={proxy_extension_dir}')
            args.insert(-1, f'--load-extension={proxy_extension_dir}')
            self._log(f'[Browser:{self.lane_id}] Proxy auth extension ready')
        self._chrome_proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._log(f'[Browser:{self.lane_id}] Spawned Chrome PID: {self._chrome_proc.pid} on port {self._debug_port}')
        return self._wait_for_debug_endpoint(self._debug_port)

    def _spawn_chrome(self, chrome_path: str) -> str:
        last_error = None
        for attempt in range(1, 4):
            try:
                return self._spawn_chrome_once(chrome_path)
            except Exception as exc:
                last_error = exc
                self._log(f'[Browser:{self.lane_id}] Chrome debug startup failed attempt={attempt}/3: {exc}')
                self._terminate_chrome_process()
                time.sleep(2.0 * attempt)
        raise BananaClientError(f'Could not start Chrome debug session after 3 attempts: {last_error}')

    async def _prepare_flow_page_async(self, page, force_navigate: bool = False) -> None:
        current_url = page.url or ''
        if force_navigate or not self._is_flow_url(current_url):
            self._log(f'[Flow:{self.lane_id}] Navigating to Flow from: {current_url or "blank"}')
            try:
                await page.goto(FLOW_URL, wait_until='domcontentloaded', timeout=60000)
            except Exception as exc:
                self._log(f'[Flow:{self.lane_id}] Initial Flow navigation failed: {exc}')
                try:
                    await page.evaluate("url => { window.location.replace(url); }", FLOW_URL)
                except Exception:
                    pass
                await page.wait_for_load_state('domcontentloaded', timeout=60000)

        current_url = page.url or ''
        if not self._is_flow_url(current_url):
            try:
                self._log(f'[Flow:{self.lane_id}] Forcing Flow reload from: {current_url or "blank"}')
                await page.goto(FLOW_URL, wait_until='domcontentloaded', timeout=60000)
                current_url = page.url or ''
            except Exception:
                pass
        if not self._is_flow_url(current_url):
            raise BananaClientError(f'Flow page navigation failed: {current_url or "blank"}')

        try:
            await page.wait_for_function(RECAPTCHA_READY_JS, timeout=30000)
        except Exception:
            self._log(f'[Flow:{self.lane_id}] reCAPTCHA not ready, retrying Flow navigation once...')
            await page.goto(FLOW_URL, wait_until='domcontentloaded', timeout=60000)
            await page.wait_for_function(RECAPTCHA_READY_JS, timeout=20000)

    async def _ensure_internal_async(self):
        async with self._ensure_lock:
            if self._page and not self._page.is_closed():
                return self._page

            chrome_path = self._find_chrome()
            self._log(f'[Browser:{self.lane_id}] Chrome executable: {chrome_path}')
            self._log(f'[Browser:{self.lane_id}] User data dir: {self._user_data_dir}')
            if self.proxy_server:
                self._log(f'[Browser:{self.lane_id}] Chrome proxy: {redact_proxy_for_log(self.proxy_server)}')

            self._user_data_dir.mkdir(parents=True, exist_ok=True)

            self._pw = await async_playwright().start()
            ws_endpoint = await asyncio.to_thread(self._spawn_chrome, chrome_path)
            self._browser = await self._pw.chromium.connect_over_cdp(ws_endpoint)
            contexts = self._browser.contexts
            self._browser_context = contexts[0] if contexts else await self._browser.new_context()
            pages = self._browser_context.pages
            candidate_page = None
            for existing_page in pages:
                if self._is_flow_url(existing_page.url):
                    candidate_page = existing_page
                    break
            if candidate_page is None:
                for existing_page in pages:
                    if not self._is_browser_internal_url(existing_page.url):
                        candidate_page = existing_page
                        break
            if candidate_page is None:
                candidate_page = await self._browser_context.new_page()
            self._page = candidate_page
            await self._ensure_proxy_auth_async(self._page)
            await self._prepare_flow_page_async(self._page, force_navigate=True)
            try:
                await self._page.bring_to_front()
            except Exception:
                pass
            for existing_page in list(self._browser_context.pages):
                if existing_page is self._page:
                    continue
                if self._is_browser_internal_url(existing_page.url):
                    try:
                        await existing_page.close()
                    except Exception:
                        pass
            self._log(f'[Flow:{self.lane_id}] Ready at: {self._page.url}')
            return self._page

    async def _clear_browser_data_internal_async(self) -> None:
        page = await self._ensure_internal_async()
        async with self._ensure_lock:
            if page.is_closed():
                page = await self._browser_context.new_page()
                self._page = page
            context = self._browser_context
            if context is None:
                raise BananaClientError('Browser context is not ready for clear data')

            self._log(f'[Browser:{self.lane_id}] Clearing Chrome data without restarting Chrome')
            try:
                await context.clear_cookies()
            except Exception as exc:
                self._log(f'[Browser:{self.lane_id}] clear_cookies failed: {exc}')

            session = None
            try:
                session = await context.new_cdp_session(page)
                for command in ('Network.clearBrowserCookies', 'Network.clearBrowserCache'):
                    try:
                        await session.send(command)
                    except Exception as exc:
                        self._log(f'[Browser:{self.lane_id}] {command} failed: {exc}')

                for origin in (
                    'https://labs.google',
                    'https://flow.google',
                    'https://aistudio.google.com',
                    'https://accounts.google.com',
                    'https://www.google.com',
                ):
                    try:
                        await session.send('Storage.clearDataForOrigin', {
                            'origin': origin,
                            'storageTypes': 'all',
                        })
                    except Exception as exc:
                        self._log(f'[Browser:{self.lane_id}] clearDataForOrigin failed origin={origin}: {exc}')
            finally:
                try:
                    if session:
                        await session.detach()
                except Exception:
                    pass

            try:
                await page.evaluate("""
                    () => {
                        try { localStorage.clear(); } catch (e) {}
                        try { sessionStorage.clear(); } catch (e) {}
                        try {
                            if (window.indexedDB && indexedDB.databases) {
                                indexedDB.databases().then(dbs => dbs.forEach(db => db.name && indexedDB.deleteDatabase(db.name))).catch(() => {});
                            }
                        } catch (e) {}
                    }
                """)
            except Exception as exc:
                self._log(f'[Browser:{self.lane_id}] in-page storage clear failed: {exc}')

            for existing_page in list(context.pages):
                if existing_page is page:
                    continue
                try:
                    await existing_page.close()
                except Exception:
                    pass

            self._cdp_session = None
            self._page = page
            await self._prepare_flow_page_async(page, force_navigate=True)
            try:
                await page.bring_to_front()
            except Exception:
                pass
            self._log(f'[Browser:{self.lane_id}] Chrome data cleared; reused PID={getattr(self._chrome_proc, "pid", None)}')

    def clear_browser_data(self) -> None:
        self._run_coro(self._clear_browser_data_internal_async())

    async def _evaluate_create_image_internal_async(self, config: dict) -> dict:
        page = await self._ensure_internal_async()
        return await page.evaluate(EXECUTE_IMAGE_JS, config)

    async def _close_internal_async(self):
        try:
            if self._page and not self._page.is_closed():
                await self._page.close()
        except Exception:
            pass
        self._page = None

        try:
            if self._browser_context:
                await self._browser_context.close()
        except Exception:
            pass
        self._browser_context = None
        self._cdp_session = None

        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        self._browser = None

        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._pw = None

        self._terminate_chrome_process()

    def _browser_thread_main(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ensure_lock = asyncio.Lock()
        self._thread_ready.set()
        self._loop.run_forever()
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self._loop.run_until_complete(self._close_internal_async())
        self._loop.close()

    def _ensure_thread(self):
        with self._thread_start_lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread_ready.clear()
            self._thread = threading.Thread(target=self._browser_thread_main, name=f'FlowBrowser-{self.lane_id}', daemon=True)
            self._thread.start()
            self._thread_ready.wait()

    def _run_coro(self, coro):
        self._ensure_thread()
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    def ensure_ready(self):
        return self._run_coro(self._ensure_internal_async())

    def evaluate_create_image(self, config: dict) -> dict:
        return self._run_coro(self._evaluate_create_image_internal_async(config))

    def close(self, remove_profile: bool = False):
        if not self._thread or not self._thread.is_alive() or not self._loop:
            if remove_profile and self._user_data_dir.exists():
                try:
                    shutil.rmtree(self._user_data_dir)
                except Exception:
                    pass
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)
        self._thread = None
        self._loop = None
        if remove_profile and self._user_data_dir.exists():
            try:
                shutil.rmtree(self._user_data_dir)
            except Exception:
                pass


class BananaImageClient:
    def __init__(self, logger: Callable[[str], None], lane_id: str = 'shared', user_data_dir: Optional[str] = None, proxy_server: Optional[str] = None):
        self.logger = logger
        self.lane_id = lane_id
        self.browser = FlowBrowser(logger=logger, lane_id=lane_id, user_data_dir=user_data_dir, proxy_server=proxy_server)
        self._http = _build_http_client()

    def _log(self, message: str) -> None:
        if self.logger:
            self.logger(message)

    def clear_browser_data(self) -> None:
        self.browser.clear_browser_data()

    def _extract_encoded_image(self, data: dict) -> Optional[str]:
        if not isinstance(data, dict):
            return None
        direct = data.get('encodedImage') or data.get('encoded_image')
        if isinstance(direct, str) and direct:
            return direct

        media = data.get('media')
        if isinstance(media, list):
            for item in media:
                found = self._extract_encoded_image(item)
                if found:
                    return found
        elif isinstance(media, dict):
            found = self._extract_encoded_image(media)
            if found:
                return found

        image = data.get('image')
        if isinstance(image, dict):
            found = self._extract_encoded_image(image)
            if found:
                return found

        generated = data.get('generatedImage')
        if isinstance(generated, dict):
            found = self._extract_encoded_image(generated)
            if found:
                return found

        operation = data.get('operation')
        if isinstance(operation, dict):
            found = self._extract_encoded_image(operation.get('metadata') or {})
            if found:
                return found

        operations = data.get('operations') or []
        if isinstance(operations, list):
            for item in operations:
                found = self._extract_encoded_image(item)
                if found:
                    return found
        return None

    def _requests_headers(self, access_token: str) -> dict:
        return {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'text/plain;charset=UTF-8',
            'Accept': '*/*',
            'Origin': 'https://labs.google',
            'Referer': 'https://labs.google/',
        }

    def _project_id(self, project_id: Optional[str] = None) -> str:
        return (project_id or PROJECT_ID).strip()

    def _image_create_url(self, project_id: Optional[str] = None) -> str:
        return IMAGE_CREATE_URL_TEMPLATE.format(project_id=self._project_id(project_id))

    def fetch_token_info(self, access_token: str) -> dict:
        url = 'https://aisandbox-pa.googleapis.com/v1/credits'
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Accept': '*/*',
        }
        response = self._http.get(url, headers=headers, timeout=60)
        response.raise_for_status()
        data = response.json()
        return {
            'credits': data.get('credits', 0),
            'userPaygateTier': data.get('userPaygateTier', 'UNKNOWN'),
            'serviceTier': data.get('serviceTier', 'UNKNOWN'),
            'sku': data.get('sku'),
        }

    def extract_media_id(self, data: dict) -> Optional[str]:
        if not isinstance(data, dict):
            return None

        direct = (
            data.get('mediaId')
            or data.get('name')
            or data.get('id')
            or data.get('mediaGenerationId')
        )
        if isinstance(direct, str) and direct:
            return direct

        media = data.get('media')
        if isinstance(media, dict):
            nested = (
                media.get('mediaId')
                or media.get('name')
                or media.get('id')
                or media.get('mediaGenerationId')
            )
            if isinstance(nested, str) and nested:
                return nested
        elif isinstance(media, list):
            for item in media:
                found = self.extract_media_id(item)
                if found:
                    return found

        operations = data.get('operations') or []
        if isinstance(operations, list):
            for item in operations:
                found = self.extract_media_id(item)
                if found:
                    return found

        operation = data.get('operation')
        if isinstance(operation, dict):
            metadata = operation.get('metadata') or {}
            found = self.extract_media_id(metadata)
            if found:
                return found

        image = data.get('image')
        if isinstance(image, dict):
            found = self.extract_media_id(image)
            if found:
                return found

        generated = data.get('generatedImage')
        if isinstance(generated, dict):
            found = self.extract_media_id(generated)
            if found:
                return found

        return None

    def upload_reference(self, access_token: str, file_path: str, project_id: Optional[str] = None) -> str:
        mime_type = mimetypes.guess_type(file_path)[0] or 'image/jpeg'
        file_name = os.path.basename(file_path)
        with open(file_path, 'rb') as handle:
            image_b64 = base64.b64encode(handle.read()).decode('ascii')
        effective_project_id = self._project_id(project_id)

        body = {
            'clientContext': {
                'projectId': effective_project_id,
                'tool': 'PINHOLE',
            },
            'fileName': file_name,
            'imageBytes': image_b64,
            'isHidden': False,
            'isUserUploaded': True,
            'mimeType': mime_type,
        }

        response = self._http.post(
            FLOW_UPLOAD_URL,
            headers=self._requests_headers(access_token),
            content=json.dumps(body),
            timeout=120,
        )
      
        response.raise_for_status()
        data = response.json()
      

        media_id = (
            data.get('mediaId')
            or (data.get('media') or {}).get('mediaId')
            or data.get('name')
            or (data.get('media') or {}).get('name')
            or data.get('id')
            or ((data.get('mediaGenerationId') or {}).get('mediaGenerationId') if isinstance(data.get('mediaGenerationId'), dict) else None)
            or data.get('mediaGenerationId')
        )
        logging.info('UPLOAD_DEBUG extracted_media_id=%s', media_id)
        self._log(f'UPLOAD_DEBUG extracted_media_id={media_id}')
        if not media_id:
            raise BananaClientError('Upload succeeded but mediaId was not found in response')
        return media_id

    def create_image(self, access_token: str, prompt: str, aspect_ratio: str = '16:9', media_ids: Optional[list[str]] = None, model: str = 'GEM_PIX_2', project_id: Optional[str] = None) -> dict:
        api_aspect = 'IMAGE_ASPECT_RATIO_LANDSCAPE'
        if aspect_ratio == '9:16':
            api_aspect = 'IMAGE_ASPECT_RATIO_PORTRAIT'
        elif aspect_ratio == '1:1':
            api_aspect = 'IMAGE_ASPECT_RATIO_SQUARE'

        normalized_model = 'GEM_PIX_2' if model in ('', 'GEM_PIX', 'GEM_PIX_2', None) else model
        session_id = f';{int(time.time() * 1000)}'
        seed = random.randint(1, 999999999)
        effective_project_id = self._project_id(project_id)

        client_context = {
            'projectId': effective_project_id,
            'sessionId': session_id,
            'tool': 'PINHOLE',
        }

        request_obj = {
            'clientContext': dict(client_context),
            'seed': seed,
            'imageModelName': normalized_model,
            'imageAspectRatio': api_aspect,
            'structuredPrompt': {
                'parts': [{'text': prompt or ''}],
            },
            'imageInputs': [],
        }

        if media_ids:
            request_obj['imageInputs'] = [
                {
                    'imageInputType': 'IMAGE_INPUT_TYPE_REFERENCE',
                    'name': media_id,
                }
                for media_id in media_ids
            ]

        payload = {
            'clientContext': client_context,
            'requests': [request_obj],
        }

        config = {
            'siteKey': SITE_KEY,
            'action': 'IMAGE_GENERATION',
            'apiUrl': self._image_create_url(effective_project_id),
            'method': 'POST',
            'payloadJson': json.dumps(payload),
            'bearerToken': access_token,
        }

        result = self.browser.evaluate_create_image(config)
        if not result.get('ok'):
            raise BananaClientError(self._format_error(result))

        data = result.get('data') or {}
        generated = (((data.get('media') or [{}])[0].get('image') or {}).get('generatedImage') or {})
        if generated.get('fifeUrl'):
            return {'type': 'sync', 'downloadUrl': generated['fifeUrl'], 'raw': data}

        operations = data.get('operations') or []
        if operations:
            operation = (operations[0].get('operation') or {})
            scene_id = (((operations[0].get('operation') or {}).get('metadata') or {}).get('image') or {}).get('sceneId') or str(uuid.uuid4())
            if operation.get('name'):
                return {
                    'type': 'async',
                    'operationName': operation['name'],
                    'sceneId': scene_id,
                    'raw': data,
                }

        raise BananaClientError('No image URL or async operation found in response')

    def _extract_video_async_result(self, data: dict) -> Optional[dict]:
        if not isinstance(data, dict):
            return None

        operations = data.get('operations') or []
        operation = {}
        if operations:
            first_operation = operations[0] or {}
            operation = first_operation.get('operation') or first_operation or {}

        media_items = data.get('media') or []
        media_item = media_items[0] if isinstance(media_items, list) and media_items else {}
        if not isinstance(media_item, dict):
            media_item = {}

        workflows = data.get('workflows') or []
        workflow = workflows[0] if isinstance(workflows, list) and workflows else {}
        if not isinstance(workflow, dict):
            workflow = {}

        workflow_metadata = workflow.get('metadata') or {}
        media_video_operation_name = (((media_item.get('video') or {}).get('operation') or {}).get('name'))
        media_name = media_item.get('name')
        operation_name = (
            operation.get('name')
            or media_video_operation_name
            or media_name
            or data.get('operationName')
            or workflow.get('name')
            or media_item.get('workflowId')
        )
        if not operation_name:
            return None

        scene_id = (
            (((operation.get('metadata') or {}).get('image') or {}).get('sceneId'))
            or data.get('sceneId')
            or media_name
            or workflow_metadata.get('primaryMediaId')
            or ''
        )
        return {
            'type': 'async',
            'operationName': operation_name,
            'sceneId': scene_id,
            'raw': data,
        }

    def create_video_from_text(
        self,
        access_token: str,
        prompt: str,
        aspect_ratio: str = '16:9',
        tier: str = 'PAYGATE_TIER_TWO',
        model_speed: str = 'relaxed',
        project_id: Optional[str] = None,
    ) -> dict:
        api_aspect = 'VIDEO_ASPECT_RATIO_PORTRAIT' if aspect_ratio == '9:16' else 'VIDEO_ASPECT_RATIO_LANDSCAPE'
        video_model_key = get_video_model_key(aspect_ratio, tier=tier, model_speed=model_speed)
        effective_project_id = self._project_id(project_id)
        payload = {
            'mediaGenerationContext': {
                'batchId': str(uuid.uuid4()),
                'audioFailurePreference': 'BLOCK_SILENCED_VIDEOS',
            },
            'clientContext': {
                'projectId': effective_project_id,
                'tool': 'PINHOLE',
                'userPaygateTier': tier,
                'sessionId': f';{int(time.time() * 1000)}',
            },
            'requests': [{
                'aspectRatio': api_aspect,
                'textInput': {
                    'structuredPrompt': {
                        'parts': [{'text': prompt or ''}],
                    }
                },
                'videoModelKey': video_model_key,
                'metadata': {},
                'seed': random.randint(10000, 99999),
            }],
            'useV2ModelConfig': True,
        }

        config = {
            'siteKey': SITE_KEY,
            'action': 'VIDEO_GENERATION',
            'apiUrl': VIDEO_TEXT_CREATE_URL,
            'method': 'POST',
            'payloadJson': json.dumps(payload),
            'bearerToken': access_token,
        }

        result = self.browser.evaluate_create_image(config)
        if not result.get('ok'):
            raise BananaClientError(self._format_error(result))

        data = result.get('data') or {}
        async_result = self._extract_video_async_result(data)
        if async_result:
            return async_result
        raise BananaClientError('No text-to-video operation name in response')

    def create_video_from_image(
        self,
        access_token: str,
        prompt: str,
        media_id: str,
        aspect_ratio: str = '16:9',
        tier: str = 'PAYGATE_TIER_TWO',
        model_speed: str = 'relaxed',
        project_id: Optional[str] = None,
    ) -> dict:
        api_aspect = 'VIDEO_ASPECT_RATIO_PORTRAIT' if aspect_ratio == '9:16' else 'VIDEO_ASPECT_RATIO_LANDSCAPE'
        video_model_key = get_video_model_key(aspect_ratio, tier=tier, model_speed=model_speed)
        effective_project_id = self._project_id(project_id)
        payload = {
            'mediaGenerationContext': {
                'batchId': str(uuid.uuid4()),
                'audioFailurePreference': 'BLOCK_SILENCED_VIDEOS',
            },
            'clientContext': {
                'projectId': effective_project_id,
                'tool': 'PINHOLE',
                'userPaygateTier': tier,
                'sessionId': f';{int(time.time() * 1000)}',
            },
            'requests': [{
                'aspectRatio': api_aspect,
                'seed': random.randint(10000, 99999),
                'textInput': {
                    'structuredPrompt': {
                        'parts': [{'text': prompt or ''}],
                    }
                },
                'videoModelKey': video_model_key,
                'metadata': {'sceneId': ''},
                'startImage': {
                    'mediaId': media_id,
                    'cropCoordinates': {
                        'top': 0,
                        'left': 0,
                        'bottom': 1,
                        'right': 1,
                    }
                }
            }],
            'useV2ModelConfig': True,
        }

        config = {
            'siteKey': SITE_KEY,
            'action': 'VIDEO_GENERATION',
            'apiUrl': VIDEO_CREATE_URL,
            'method': 'POST',
            'payloadJson': json.dumps(payload),
            'bearerToken': access_token,
        }

        result = self.browser.evaluate_create_image(config)
        if not result.get('ok'):
            raise BananaClientError(self._format_error(result))

        data = result.get('data') or {}
        async_result = self._extract_video_async_result(data)
        if async_result:
            return async_result
        raise BananaClientError('No video operation name in response')

    def create_video_from_start_end_images(
        self,
        access_token: str,
        prompt: str,
        start_media_id: str,
        end_media_id: str,
        aspect_ratio: str = '16:9',
        tier: str = 'PAYGATE_TIER_TWO',
        model_speed: str = 'relaxed',
        project_id: Optional[str] = None,
    ) -> dict:
        api_aspect = 'VIDEO_ASPECT_RATIO_PORTRAIT' if aspect_ratio == '9:16' else 'VIDEO_ASPECT_RATIO_LANDSCAPE'
        video_model_key = get_video_model_key(aspect_ratio, tier=tier, model_speed=model_speed)
        effective_project_id = self._project_id(project_id)
        payload = {
            'mediaGenerationContext': {
                'batchId': str(uuid.uuid4()),
                'audioFailurePreference': 'BLOCK_SILENCED_VIDEOS',
            },
            'clientContext': {
                'projectId': effective_project_id,
                'tool': 'PINHOLE',
                'userPaygateTier': tier,
                'sessionId': f';{int(time.time() * 1000)}',
            },
            'requests': [{
                'aspectRatio': api_aspect,
                'seed': random.randint(10000, 99999),
                'textInput': {
                    'structuredPrompt': {
                        'parts': [{'text': prompt or ''}],
                    }
                },
                'videoModelKey': video_model_key,
                'metadata': {},
                'startImage': {
                    'mediaId': start_media_id,
                },
                'endImage': {
                    'mediaId': end_media_id,
                },
            }],
            'useV2ModelConfig': True,
        }

        config = {
            'siteKey': SITE_KEY,
            'action': 'VIDEO_GENERATION',
            'apiUrl': VIDEO_START_END_CREATE_URL,
            'method': 'POST',
            'payloadJson': json.dumps(payload),
            'bearerToken': access_token,
        }

        result = self.browser.evaluate_create_image(config)
        if not result.get('ok'):
            raise BananaClientError(self._format_error(result))

        data = result.get('data') or {}
        async_result = self._extract_video_async_result(data)
        if async_result:
            return async_result
        raise BananaClientError('No video start/end operation name in response')

    def create_video_from_reference_image(
        self,
        access_token: str,
        prompt: str,
        media_id: str,
        aspect_ratio: str = '16:9',
        tier: str = 'PAYGATE_TIER_TWO',
        model_speed: str = 'relaxed',
        project_id: Optional[str] = None,
    ) -> dict:
        api_aspect = 'VIDEO_ASPECT_RATIO_PORTRAIT' if aspect_ratio == '9:16' else 'VIDEO_ASPECT_RATIO_LANDSCAPE'
        video_model_key = get_reference_video_model_key(aspect_ratio, tier=tier, model_speed=model_speed)
        effective_project_id = self._project_id(project_id)
        seed = random.randint(10000, 99999)
        payload = {
            'mediaGenerationContext': {
                'batchId': str(uuid.uuid4()),
                'audioFailurePreference': 'BLOCK_SILENCED_VIDEOS',
            },
            'clientContext': {
                'projectId': effective_project_id,
                'tool': 'PINHOLE',
                'userPaygateTier': tier,
                'sessionId': f';{int(time.time() * 1000)}',
            },
            'requests': [{
                'aspectRatio': api_aspect,
                'seed': seed,
                'textInput': {
                    'structuredPrompt': {
                        'parts': [{'text': prompt or ''}],
                    }
                },
                'videoModelKey': video_model_key,
                'metadata': {},
                'referenceImages': [{
                    'mediaId': media_id,
                    'imageUsageType': 'IMAGE_USAGE_TYPE_ASSET',
                }],
            }],
            'useV2ModelConfig': True,
        }

        config = {
            'siteKey': SITE_KEY,
            'action': 'VIDEO_GENERATION',
            'apiUrl': VIDEO_REFERENCE_IMAGES_CREATE_URL,
            'method': 'POST',
            'payloadJson': json.dumps(payload),
            'bearerToken': access_token,
        }

        self._log(
            'stage=reference-video-submit-debug '
            f'endpoint={VIDEO_REFERENCE_IMAGES_CREATE_URL} project_id={effective_project_id} '
            f'aspect={api_aspect} model_key={video_model_key} media_id={media_id} '
            f'seed={seed} tier={tier} prompt_len={len(prompt or "")} '
            f'batch_id={payload["mediaGenerationContext"]["batchId"]}'
        )
        result = self.browser.evaluate_create_image(config)
        self._log(
            'stage=reference-video-submit-response '
            f'ok={result.get("ok")} status={result.get("status")} '
            f'error={result.get("error", "")} response={json.dumps(result.get("data"), ensure_ascii=False)[:3000]}'
        )
        if not result.get('ok'):
            raise BananaClientError(self._format_error(result))

        data = result.get('data') or {}
        async_result = self._extract_video_async_result(data)
        if async_result:
            return async_result
        raise BananaClientError('No video reference image operation name in response')

    def upsample_image_4k(
        self,
        access_token: str,
        media_id: str,
        target_resolution: str = 'UPSAMPLE_IMAGE_RESOLUTION_4K',
        tier: str = 'PAYGATE_TIER_TWO',
        project_id: Optional[str] = None,
    ) -> dict:
        effective_project_id = self._project_id(project_id)
        payload = {
            'mediaId': media_id,
            'targetResolution': target_resolution,
            'clientContext': {
                'projectId': effective_project_id,
                'tool': 'PINHOLE',
                'userPaygateTier': tier,
                'sessionId': f';{int(time.time() * 1000)}',
            },
        }

        config = {
            'siteKey': SITE_KEY,
            'action': 'IMAGE_GENERATION',
            'apiUrl': UPSAMPLE_IMAGE_URL,
            'method': 'POST',
            'payloadJson': json.dumps(payload),
            'bearerToken': access_token,
        }


        result = self.browser.evaluate_create_image(config)
        if not result.get('ok'):
            raise BananaClientError(self._format_error(result))

        data = result.get('data') or {}
        encoded_image = self._extract_encoded_image(data)
        download_url = (
            data.get('downloadUrl')
            or data.get('fifeUrl')
            or data.get('mediaUri')
            or (((data.get('media') or [{}])[0].get('image') or {}).get('fifeUrl'))
            or (((data.get('media') or [{}])[0].get('image') or {}).get('generatedImage') or {}).get('fifeUrl')
            or (((data.get('media') or {}).get('image') or {}).get('fifeUrl') if isinstance(data.get('media'), dict) else None)
        )
        if encoded_image:
            return {
                'type': 'sync',
                'downloadUrl': encoded_image,
                'link': 'encodedImage',
                'raw': data,
            }
        if download_url:
            return {
                'type': 'sync',
                'downloadUrl': download_url,
                'link': download_url,
                'raw': data,
            }

        operation = (data.get('operation') or {})
        operations = data.get('operations') or []
        if operations:
            operation = (operations[0].get('operation') or operations[0] or {})
        operation_name = operation.get('name') or data.get('operationName')
        if operation_name:
            scene_id = (((operation.get('metadata') or {}).get('image') or {}).get('sceneId')) or data.get('sceneId') or str(uuid.uuid4())
            return {
                'type': 'async',
                'operationName': operation_name,
                'sceneId': scene_id,
                'downloadUrl': download_url,
                'link': download_url,
                'raw': data,
            }

        raise BananaClientError('No upsample image URL or async operation found in response')

    def poll_image(self, access_token: str, operation_name: str, scene_id: str) -> dict:
        body = {
            'operations': [
                {
                    'operation': {'name': operation_name},
                    'sceneId': scene_id,
                }
            ]
        }
        response = self._http.post(
            POLL_URL,
            headers=self._requests_headers(access_token),
            content=json.dumps(body),
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()

        operations = data.get('operations') or []
        if not operations:
            return {'done': False, 'failed': False, 'raw': data}

        operation = operations[0]
        status = operation.get('status')
        if status == 'MEDIA_GENERATION_STATUS_SUCCESSFUL':
            op_meta = ((operation.get('operation') or {}).get('metadata') or {})
            encoded_image = self._extract_encoded_image(operation) or self._extract_encoded_image(op_meta) or self._extract_encoded_image(data)
            download_url = (
                encoded_image
                or ((operation.get('media') or [{}])[0].get('mediaUri'))
                or ((op_meta.get('video') or {}).get('fifeUrl'))
                or ((op_meta.get('image') or {}).get('fifeUrl'))
            )

            return {'done': True, 'failed': False, 'downloadUrl': download_url, 'raw': data}
        if status == 'MEDIA_GENERATION_STATUS_FAILED':
            self._log(f'stage=poll-failed-raw response={json.dumps(data, ensure_ascii=False)}')
            return {'done': True, 'failed': True, 'raw': data}
        return {'done': False, 'failed': False, 'raw': data}

    def save_remote_file(self, url: str, output_path: str) -> str:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        if isinstance(url, str) and not url.lower().startswith(('http://', 'https://')):
            encoded = url
            if encoded.startswith('data:image') and ',' in encoded:
                encoded = encoded.split(',', 1)[1]
            output.write_bytes(base64.b64decode(encoded))
            return str(output)

        response = self._http.get(url, timeout=120)
        response.raise_for_status()
        output.write_bytes(response.content)
        return str(output)

    def shutdown(self, remove_profile: bool = False) -> None:
        chrome_proc = getattr(self.browser, '_chrome_proc', None)
        chrome_pid = getattr(chrome_proc, 'pid', None) if chrome_proc else None
        try:
            self.browser.close(remove_profile=remove_profile)
        finally:
            if chrome_proc and chrome_proc.poll() is None:
                try:
                    chrome_proc.terminate()
                    chrome_proc.wait(timeout=5)
                except Exception:
                    try:
                        chrome_proc.kill()
                        chrome_proc.wait(timeout=5)
                    except Exception:
                        pass
            if chrome_pid:
                try:
                    subprocess.run(
                        ['taskkill', '/PID', str(chrome_pid), '/T', '/F'],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                        check=False,
                    )
                except Exception:
                    pass


    def _format_error(self, result: dict) -> str:
        data = result.get('data') or {}
        api_error = data.get('error') or {}
        status = result.get('status') or api_error.get('code') or 0
        reason = ''
        details = api_error.get('details') or []
        if details and isinstance(details[0], dict):
            reason = details[0].get('reason', '')
        message = api_error.get('message') or result.get('error') or 'Image generation failed'
        parts = []
        if status:
            parts.append(f'HTTP {status}')
        if reason:
            parts.append(reason)
        if message:
            parts.append(message)
        return ' - '.join(parts) if parts else 'Image generation failed'

