import json
import uuid
import re
import os
import io
import hashlib
import time
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import base64
import random
from core import browser_config as bcfg
from core.http_helpers import request_with_retries, HTTPError
from core import dns_helper as _dns

# ── Image Upload Cache ──
# Tránh upload lại ảnh đã upload thành công cho cùng project.
# Key: MD5(file_bytes) + project_id → Value: {media_id, timestamp}
_IMAGE_UPLOAD_CACHE = {}  # thread-safe vì GIL Python
_IMAGE_CACHE_TTL = 1800   # 30 phút — Google media_id có thể hết hạn
_IMAGE_CACHE_MAX = 200    # Giới hạn số entry cache


import logging as _logging
_logger = _logging.getLogger(__name__)

# Direct file logger — bypass logging framework
import datetime as _dt
import os as _os

def _filelog(msg: str):
    try:
        _log_path = _os.path.join(
            _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
            "logs", "veo_api.log"
        )
        _os.makedirs(_os.path.dirname(_log_path), exist_ok=True)
        ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(_log_path, "a", encoding="utf-8", errors="replace") as _f:
            _f.write(f"{ts} [VEO] {msg}\n")
    except Exception:
        pass


def _safe_print(*args, **kwargs):
    """Redirect to logger.info — never touches stdout, prevents OSError on VPS."""
    try:
        msg = " ".join(str(a) for a in args)
        _logger.info(msg)
    except Exception:
        pass



class VeoClient:

    # ── Class-level: giữ sessionId cố định per cookie (account) ──
    _cookie_session_ids: dict = {}
    _browser_runtime_clients: dict = {}
    _browser_runtime_lock = None

    T2V_URL = "https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoText"
    I2V_URL = "https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoReferenceImages"
    UPSCALE_URL = "https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoUpsampleVideo"
    POLL_URL = "https://aisandbox-pa.googleapis.com/v1/video:batchCheckAsyncVideoGenerationStatus"
    UPLOAD_URL = "https://aisandbox-pa.googleapis.com/v1:uploadUserImage"
    SESSION_URL = "https://labs.google/fx/api/auth/session"

    def __init__(self, cookie, proxy=None):
        self.proxy = self._normalize_proxy_url(proxy) if proxy else None
        if self.proxy:
            _filelog(f"[Proxy] VeoClient using proxy for browser runtime: {self.proxy}")

        # sessionId cố định per cookie (account) — giống browser
        # Reuse sessionId khi tạo client mới cho cùng cookie
        _ck = str(cookie)[:60]
        if _ck not in VeoClient._cookie_session_ids:
            VeoClient._cookie_session_ids[_ck] = f";{int(time.time() * 1000)}"
        self._session_id = VeoClient._cookie_session_ids[_ck]

        # Pre-process: nếu cookie là JSON string (từ pool), parse ra list/dict trước
        if isinstance(cookie, str):
            _s = cookie.strip()
            if _s.startswith("[") or _s.startswith("{"):
                try:
                    cookie = json.loads(_s)
                except Exception:
                    pass  # không parse được → dùng nguyên chuỗi

        normalized = None
        try:
            if isinstance(cookie, dict):
                normalized = cookie.get("value")
            elif isinstance(cookie, list):
                cookie_parts = []
                for c in cookie:
                    if isinstance(c, dict) and "name" in c and "value" in c:
                        cookie_parts.append(f"{c['name']}={c['value']}")
                if cookie_parts:
                    normalized = "; ".join(cookie_parts)
                else:
                    for c in cookie:
                        if isinstance(c, dict):
                            n = c.get("name", "")
                            if "__Secure-next-auth" in n or "session" in n.lower():
                                normalized = c.get("value")
                                break
            elif isinstance(cookie, str):
                normalized = cookie
        except Exception:
            normalized = cookie

        cookie_header = normalized
        if isinstance(normalized, str):
            c = normalized.strip()
            if (
                ";" in c
                or c.startswith("__Host-")
                or c.startswith("__Secure-next-auth.callback-url")
            ):
                cookie_header = c
            elif c.startswith("ey") and "=" not in c[:20]:
                cookie_header = f"__Secure-next-auth.session-token={c}"
            elif c.startswith("__Secure-next-auth.session-token="):
                cookie_header = c
            elif "ya29" in c:
                cookie_header = c # Allow direct Bearer
        else:
            cookie_header = cookie

        # Only fall back to full_cookie from browser_config when no cookie is explicitly passed
        if not cookie_header:
            full_cookie = bcfg.get("full_cookie")
            if full_cookie:
                cookie_header = full_cookie
                _safe_print("[VeoClient] Falling back to full_cookie from browser_config.json")
        else:
            _safe_print("[VeoClient] Using explicitly passed cookie per-request")

        self.cookie = cookie_header
        self.access_token = None
        self._t2v_auth_variant = None
        self._i2v_auth_variant = None
        self._last_error_detail = None  # Lưu lý do lỗi cuối cùng từ Google API
        self._current_project_id = None  # Lưu project ID để xây Referer chính xác

        ua = bcfg.get(
            "user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        )

        # Auto-generate x-browser-validation if not in config
        bval = bcfg.get("x_browser_validation")
        if not bval:
            try:
                from generate_bval import generate_validation_header

                bval = generate_validation_header(ua, "windows")
            except Exception:
                pass

        self.base_headers = {
            "User-Agent": ua,
            "Referer": "https://labs.google/fx/vi/tools/flow",
            "Origin": "https://labs.google",
            "Content-Type": "application/json",
            "x-browser-channel": bcfg.get("x_browser_channel", "stable"),
            "x-browser-validation": bval,
            "x-client-data": bcfg.get("x_client_data"),
            "Cookie": cookie_header,
        }

        self.get_session_token()

    @staticmethod
    def _normalize_proxy_url(proxy):
        if not proxy:
            return None
        p = str(proxy).strip()
        if not p:
            return None
        if "://" not in p:
            p = f"http://{p}"
        return p

    def _build_referer(self, project_id=None):
        """Xây Referer URL giống browser thật: https://labs.google/fx/vi/tools/flow/project/{id}"""
        pid = project_id or self._current_project_id
        if pid:
            return f"https://labs.google/fx/vi/tools/flow/project/{pid}"
        return "https://labs.google/fx/vi/tools/flow"

    def get_session_token(self, warm_flow: bool = False):
        try:
            _safe_print("[Auth] Getting access token from session...")
            if warm_flow:
                try:
                    warmed = self.warmup_browser_runtime()
                    _safe_print(f"[Auth] Flow warmup before session: {'OK' if warmed else 'SKIPPED/FAILED'}")
                except Exception as warm_err:
                    _safe_print(f"[Auth] Flow warmup failed before session: {warm_err}")
            # Resolve labs.google qua DoH + proxy (nếu có), fallback None = dùng system DNS
            curl_resolve = _dns.get_curl_resolve("labs.google", proxy=self.proxy)

            resp = None

            # ── Build headers giống browser thật (same-origin request tới labs.google) ──
            session_headers = {
                "accept": "*/*",
                "accept-language": bcfg.get(
                    "accept_language",
                    "vi-VN,vi;q=0.9,fr-FR;q=0.8,fr;q=0.7,en-US;q=0.6,en;q=0.5",
                ),
                "content-type": "application/json",
                "priority": "u=1, i",
                "referer": self._build_referer(),
                "sec-ch-ua": bcfg.get(
                    "sec_ch_ua",
                    '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
                ),
                "sec-ch-ua-mobile": bcfg.get("sec_ch_ua_mobile", "?0"),
                "sec-ch-ua-platform": bcfg.get("sec_ch_ua_platform", '"Windows"'),
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "user-agent": self.base_headers.get(
                    "User-Agent",
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
                ),
            }
            # Chỉ thêm Cookie nếu có
            if self.cookie:
                session_headers["Cookie"] = self.cookie
            # Loại bỏ key có value None/empty
            session_headers = {k: v for k, v in session_headers.items() if v}

            # ── Dùng requests tiêu chuẩn (không curl_cffi) ──
            try:
                import requests as _req
                resp = _req.get(
                    self.SESSION_URL,
                    headers=session_headers,
                    proxies=None,
                    timeout=(10, 30),
                    verify=False,
                )
            except Exception as req_err:
                _safe_print(f"[Auth] requests failed: {req_err}")
                return False

            if resp is None:
                _safe_print("[Auth] ERROR: Không lấy được access_token. Cookie hết hạn?")
                return False

            _cookie_snip = (self.cookie[:60] + "...") if self.cookie and len(self.cookie) > 60 else (self.cookie or "<NONE>")
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except Exception:
                    _safe_print(f"[Auth] Cannot parse JSON from response (cookie={_cookie_snip})")
                    return False
                token = data.get("access_token") if isinstance(data, dict) else None
                user = data.get("user") if isinstance(data, dict) else None
                email = (user.get("email") if isinstance(user, dict) else "") or ""
                expires = (data.get("expires") if isinstance(data, dict) else "") or ""
                if not (
                    isinstance(user, dict)
                    and email.strip()
                    and isinstance(expires, str)
                    and expires.strip()
                    and isinstance(token, str)
                    and token.startswith("ya29")
                ):
                    self.access_token = None
                    _safe_print(
                        "[Auth] Invalid session response shape; account needs re-login or must be skipped "
                        f"(cookie={_cookie_snip}, has_user={isinstance(user, dict)}, "
                        f"has_email={bool(email.strip())}, has_expires={bool(str(expires).strip())}, "
                        f"token_prefix={(str(token)[:8] if token else '<NONE>')})"
                    )
                    return False

                self.access_token = token
                _safe_print(f"[Auth] Token OK: {self.access_token[:20]}... | account={email}")
                self._account_email = email  # lưu lại để debug
                self._session_expires = expires
                return True
            text = resp.text if hasattr(resp, "text") else ""
            _safe_print(f"[Auth] FAILED status={resp.status_code} cookie={_cookie_snip} | response: {text[:400]}")
            return False
        except HTTPError as he:
            _safe_print(f"[Auth] HTTPError: {he}")
            return False
        except Exception as e:
            _safe_print(f"[Auth] Exception: {e}")
            return False

    @staticmethod
    def _normalize_aspect(aspect):
        portrait_values = (
            "ASPECT_RATIO_9_16",
            "VIDEO_ASPECT_RATIO_9_16",
            "9_16",
            "9:16",
            "portrait",
        )
        landscape_values = (
            "ASPECT_RATIO_16_9",
            "VIDEO_ASPECT_RATIO_16_9",
            "16_9",
            "16:9",
            "landscape",
        )
        if aspect in portrait_values:
            return "VIDEO_ASPECT_RATIO_PORTRAIT"
        if aspect in landscape_values:
            return "VIDEO_ASPECT_RATIO_LANDSCAPE"
        return aspect

    def _build_har_headers(self):
        headers = {
            "accept": "*/*",
            "accept-encoding": "gzip, deflate, br, zstd",
            "accept-language": bcfg.get("accept_language", "vi-VN,vi;q=0.9"),
            "content-type": "text/plain;charset=UTF-8",
            "origin": "https://labs.google",
            "priority": "u=1, i",
            "referer": "https://labs.google/",
            "sec-ch-ua": bcfg.get("sec_ch_ua"),
            "sec-ch-ua-mobile": bcfg.get("sec_ch_ua_mobile", "?0"),
            "sec-ch-ua-platform": bcfg.get("sec_ch_ua_platform", '"Windows"'),
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
            "user-agent": self.base_headers.get("User-Agent"),
            "x-browser-channel": self.base_headers.get("x-browser-channel", "stable"),
            "x-browser-copyright": bcfg.get(
                "x_browser_copyright", "Copyright 2026 Google LLC. All Rights reserved."
            ),
            "x-browser-validation": self.base_headers.get("x-browser-validation"),
            "x-browser-year": bcfg.get("x_browser_year", "2026"),
            "x-client-data": self.base_headers.get("x-client-data"),
        }
        return {k: v for k, v in headers.items() if v}

    @staticmethod
    def _cffi_post(url, headers, data, timeout=30, proxy=None):
        """POST using standard requests."""
        proxies = None
        # Connect timeout: 10s, Read timeout: theo param
        t_out = (10, timeout) if isinstance(timeout, int) else timeout
        return requests.post(
            url,
            headers=headers,
            data=data,
            timeout=t_out,
            proxies=proxies,
            verify=False,
        )

    def _build_bearer_variants(self):
        variants = []
        cookie_val = self.base_headers.get("Cookie")

        # Debug: log cookie snippet để diagnose 401
        cookie_snippet = (cookie_val[:60] + "...") if cookie_val and len(cookie_val) > 60 else (cookie_val or "<NONE>")
        _filelog(f"[Auth] cookie_val snippet: {cookie_snippet} | access_token: {self.access_token[:20] if self.access_token else '<NONE>'}")

        # Variant duy nhất: Bearer ONLY (theo yêu cầu của user)
        if self.access_token:
            variants.append({"Authorization": f"Bearer {self.access_token}"})

        if not variants:
            _filelog("[Auth] WARNING: No auth variants built — token rỗng!")
            self._last_error_detail = "Access token rỗng — get_session_token() thất bại."

        return variants

    @staticmethod
    def _generate_seeds_and_scenes(count, seed=None, scene_ids=None):
        seeds = []
        scene_ids_local = []
        for i in range(count):
            seeds.append(
                int(seed) if seed is not None else random.randint(10000, 99999)
            )
            if scene_ids and i < len(scene_ids):
                scene_ids_local.append(scene_ids[i])
            else:
                scene_ids_local.append(str(uuid.uuid4()))
        return seeds, scene_ids_local

    def _try_auth_variants(
        self,
        row,
        url,
        payload,
        har_headers,
        variants,
        tag,
        cache_attr,
        refresh_on_401=True,
    ):
        cached = getattr(self, cache_attr, None)
        if cached:
            variants.insert(0, cached)

        max_attempts = 2 if refresh_on_401 else 1
        refreshed = False

        for attempt in range(max_attempts):
            for vi, variant in enumerate(variants):
                trial = har_headers.copy()
                for k, v in variant.items():
                    if v is not None:
                        trial[k] = v
                trial = {k: v for k, v in trial.items() if v}

                # Debug log removed to prevent charmap crashes
                try:
                    resp = self._cffi_post(
                        url, trial, json.dumps(payload), proxy=self.proxy
                    )
                except Exception as e:
                    _safe_print(f"[Row {row}] [{tag}] Request exception: {e}")
                    self._last_error_detail = f"[{tag}] Network error (proxy/connection): {e}"
                    continue

                if resp is None:
                    continue

                status = getattr(resp, "status_code", None)
                try:
                    text = resp.text or ""
                except (OSError, UnicodeDecodeError):
                    text = "(unreadable response)"

                _filelog(f"[{tag}] row={row} variant={vi+1} status={status} text={text[:200]}")

                if status == 200:
                    try:
                        jr = resp.json()
                    except (OSError, UnicodeDecodeError):
                        _safe_print(f"[Row {row}] [{tag}] resp.json() encoding error")
                        self._last_error_detail = f"[{tag}] HTTP 200 but resp.json() failed with encoding error"
                        return None
                    except Exception:
                        _safe_print(f"[Row {row}] [{tag}] Response not JSON")
                        self._last_error_detail = f"[{tag}] HTTP 200 but response is not valid JSON"
                        return None
                    ops = jr.get("operations", [])
                    if ops:
                        op_name = ops[0]["operation"]["name"]
                        _safe_print(f"[Row {row}] [{tag}] Success. Op: {op_name}")
                        setattr(self, cache_attr, variant)
                        self._last_error_detail = None
                        return ops
                    _safe_print(f"[Row {row}] [{tag}] Response missing operations: {jr}")
                    self._last_error_detail = f"[{tag}] HTTP 200 but no 'operations' in response: {str(jr)[:200]}"
                    return None

                # Send error to console ONLY if it failed
                # Tránh in chi tiết JSON bẩn màn hình, chỉ in status
                error_msg_short = f"[Row {row}] [{tag}] Variant {vi+1}/{len(variants)} failed, status={status}"
                _filelog(f"{error_msg_short} text_preview={text[:80]}...")
                
                # Report error if it's the last variant
                if vi == len(variants) - 1:
                    _safe_print(error_msg_short)
                
                # Store full detail for task.error reporting
                self._last_error_detail = f"[{tag}] Google API status={status}: {text}"

                if status == 401 and refresh_on_401 and not refreshed:
                    _safe_print(f"[Row {row}] [{tag}] Refreshing token...")
                    self.get_session_token()
                    if self.access_token:
                        for v in variants:
                            if "Authorization" in v:
                                v["Authorization"] = f"Bearer {self.access_token}"
                    refreshed = True
                    break
            else:
                break

        self._last_error_detail = (
            self._last_error_detail
            or f"[{tag}] Tất cả {len(variants)} auth variant đều thất bại (proxy/network hoặc token invalid)."
        )
        _filelog(f"[{tag}] row={row} ALL {len(variants)} auth variants failed")
        _safe_print(f"[Row {row}] [{tag}] All {len(variants)} auth variants failed")
        return None

    def _browser_runtime_enabled(self) -> bool:
        return bool(bcfg.get("veo_browser_runtime_enabled", True))

    def _browser_runtime_lane_id(self) -> str:
        # One Chrome per token/account. Prefer account email when available;
        # fallback to access token/cookie fingerprint without logging secrets.
        raw = getattr(self, "_account_email", None) or self.access_token or self.cookie or "unknown"
        fp = re.sub(r"[^a-zA-Z0-9_-]+", "_", hashlib.sha1(str(raw).encode("utf-8")).hexdigest()[:12])
        return f"video-token-{fp}"

    @classmethod
    def _get_browser_runtime_client(cls, lane_id: str = None, proxy: str = None):
        import threading
        from core.banana_runtime.banana_client import BananaImageClient, get_lane_banana_client

        lane_id = lane_id or "video-token-default"
        if cls._browser_runtime_lock is None:
            cls._browser_runtime_lock = threading.Lock()
        with cls._browser_runtime_lock:
            runtime = cls._browser_runtime_clients.get(lane_id)
            if runtime is None:
                runtime = get_lane_banana_client(lane_id=lane_id, proxy=proxy, logger=_filelog)
                cls._browser_runtime_clients[lane_id] = runtime
                _filelog(f"[BrowserRuntime] Created token Chrome lane={lane_id} proxy={proxy or 'DIRECT'}")
            else:
                _filelog(f"[BrowserRuntime] Reusing token Chrome lane={lane_id}")
            return runtime

    @classmethod
    def reset_browser_runtime_client(cls, lane_id: str, remove_profile: bool = False):
        if cls._browser_runtime_lock is None:
            return
        with cls._browser_runtime_lock:
            runtime = cls._browser_runtime_clients.pop(lane_id, None)
        if runtime:
            try:
                runtime.close(remove_profile=remove_profile)
                _filelog(f"[BrowserRuntime] Closed runtime lane={lane_id}")
            except Exception as exc:
                _filelog(f"[BrowserRuntime] Close runtime lane={lane_id} failed: {exc}")

    def warmup_browser_runtime(self) -> bool:
        """Preload the per-account Flow runtime so task dispatch avoids Chrome cold-start."""
        if not self._browser_runtime_enabled():
            return False
        try:
            lane_id = self._browser_runtime_lane_id()
            runtime = self._get_browser_runtime_client(lane_id, proxy=self.proxy)
            # Use the public browser API to ensure Chrome, Flow page, and reCAPTCHA JS are ready.
            runtime.browser._run_coro(runtime.browser._ensure_internal_async())
            _filelog(f"[BrowserRuntime] Warmed token Chrome lane={lane_id}")
            return True
        except Exception as exc:
            _filelog(f"[BrowserRuntime] Warmup failed: {exc}")
            return False

    def _execute_video_payload_in_browser(self, row, tag, url, payload, cache_attr=None):
        """Execute a video create/upscale payload inside VPS Chrome.

        This keeps cookie/session-derived bearer token, reCAPTCHA execution, and
        the create request in one persistent labs.google browser context.
        """
        if not self.access_token:
            self.get_session_token()
            if not self.access_token:
                self._last_error_detail = f"[{tag}] Access token rỗng — get_session_token() thất bại."
                return None
        try:
            lane_id = self._browser_runtime_lane_id()
            runtime = self._get_browser_runtime_client(lane_id, proxy=self.proxy)
            account_label = getattr(self, "_account_email", None) or "unknown"
            _filelog(f"[{tag}][BrowserRuntime] lane submit account={account_label} lane={lane_id} proxy={self.proxy or 'DIRECT'}")
            data = runtime.execute_generation_request(
                access_token=self.access_token,
                api_url=url,
                payload=payload,
                action=bcfg.get("veo_browser_runtime_action", "VIDEO_GENERATION"),
                extra_headers={"Accept": "*/*"},
            )
            ops = data.get("operations", []) if isinstance(data, dict) else []
            if ops:
                if cache_attr:
                    setattr(self, cache_attr, {"Authorization": f"Bearer {self.access_token}"})
                self._last_error_detail = None
                _filelog(f"[{tag}][BrowserRuntime] row={row} status=OK ops={len(ops)}")
                return ops
            self._last_error_detail = f"[{tag}][BrowserRuntime] HTTP 200 but no operations: {str(data)[:300]}"
            _filelog(self._last_error_detail)
            return None
        except Exception as exc:
            self._last_error_detail = f"[{tag}][BrowserRuntime] {exc}"
            _filelog(self._last_error_detail)
            if "Target closed" in str(exc) or "debug port" in str(exc) or "Connection" in str(exc):
                try:
                    self.reset_browser_runtime_client(self._browser_runtime_lane_id())
                except Exception:
                    pass
            return None


    def create_video_t2v(
        self,
        row,
        prompt,
        project_id,
        captcha_token,
        aspect="VIDEO_ASPECT_RATIO_PORTRAIT",
        model_key=None,
        seed=None,
        scene_ids=None,
        count=1,
        reference_image_id=None,
    ):
        if not self.access_token:
            self.get_session_token()
            if not self.access_token:
                self._last_error_detail = "[T2V] Access token rỗng — get_session_token() thất bại."
                return None

        aspect = self._normalize_aspect(aspect)

        if not model_key:
            if aspect == "VIDEO_ASPECT_RATIO_PORTRAIT":
                model_key = "veo_3_1_t2v_fast_ultra"
            else:
                model_key = "veo_3_1_t2v_fast_ultra"
        _safe_print(f"[Row {row}] [T2V] Model: {model_key}")

        seeds, scene_ids_local = self._generate_seeds_and_scenes(count, seed, scene_ids)

        requests_list = []
        for i in range(count):
            req = {
                "aspectRatio": aspect,
                "seed": seeds[i],
                "textInput": {"structuredPrompt": {"parts": [{"text": prompt}]}},
                "videoModelKey": model_key,
                "metadata": {},
            }
            if reference_image_id:
                req["referenceImages"] = [
                    {
                        "mediaId": reference_image_id,
                        "imageUsageType": "IMAGE_USAGE_TYPE_ASSET",
                    }
                ]
            requests_list.append(req)

        payload = {
            "mediaGenerationContext": {"batchId": str(uuid.uuid4())},
            "clientContext": {
                "projectId": project_id,
                "tool": bcfg.get("tool", "PINHOLE"),
                "userPaygateTier": bcfg.get("user_paygate_tier", "PAYGATE_TIER_TWO"),
                "sessionId": self._session_id,
                "recaptchaContext": {
                    "token": str(captcha_token),
                    "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                },
            },
            "requests": requests_list,
            "useV2ModelConfig": True,
        }

        if self._browser_runtime_enabled():
            ops = self._execute_video_payload_in_browser(
                row, "T2V", self.T2V_URL, payload, cache_attr="_t2v_auth_variant"
            )
            if ops:
                return {
                    "op_name": ops[0]["operation"]["name"],
                    "scene_id": scene_ids_local[0],
                    "ops": ops,
                }
            if not bcfg.get("veo_browser_runtime_fallback_requests", True):
                return None

        har_headers = self._build_har_headers()
        variants = self._build_bearer_variants()

        ops = self._try_auth_variants(
            row,
            self.T2V_URL,
            payload,
            har_headers,
            variants,
            tag="T2V",
            cache_attr="_t2v_auth_variant",
            refresh_on_401=True,
        )

        if not ops:
            return None

        return {
            "op_name": ops[0]["operation"]["name"],
            "scene_id": scene_ids_local[0],
            "ops": ops,
        }

    def create_video(self, row, prompt, project_id, captcha_token, **kwargs):
        return self.create_video_t2v(row, prompt, project_id, captcha_token, **kwargs)

    def create_video_i2v(
        self,
        row,
        prompt,
        project_id,
        captcha_token,
        start_image_media_id,
        end_image_media_id=None,
        aspect="VIDEO_ASPECT_RATIO_9_16",
        seed=None,
        scene_ids=None,
        count=1,
        model_key=None,
    ):
        if not self.access_token:
            self.get_session_token()
            if not self.access_token:
                self._last_error_detail = "[I2V] Access token rỗng — get_session_token() thất bại."
                return None

        aspect = self._normalize_aspect(aspect)

        if not model_key:
            if aspect == "VIDEO_ASPECT_RATIO_PORTRAIT":
                model_key = "veo_3_1_r2v_fast_portrait_ultra"
            else:
                model_key = "veo_3_1_r2v_fast_landscape_ultra"
        _safe_print(f"[Row {row}] [I2V] Model: {model_key}")

        seeds, scene_ids_local = self._generate_seeds_and_scenes(count, seed, scene_ids)

        requests_list = []
        for i in range(count):
            reference_images = [
                {
                    "mediaId": start_image_media_id,
                    "imageUsageType": "IMAGE_USAGE_TYPE_ASSET",
                }
            ]
            # NOTE: end_image bị bỏ qua - endpoint này không hỗ trợ IMAGE_USAGE_TYPE_END_ASSET
            if end_image_media_id:
                _safe_print(
                    f"[Row {row}] [I2V] End image provided but ignored (not supported by this endpoint)"
                )
            requests_list.append(
                {
                    "aspectRatio": aspect,
                    "seed": seeds[i],
                    "textInput": {"structuredPrompt": {"parts": [{"text": prompt}]}},
                    "videoModelKey": model_key,
                    "referenceImages": reference_images,
                    "metadata": {},
                }
            )

        payload = {
            "mediaGenerationContext": {"batchId": str(uuid.uuid4())},
            "clientContext": {
                "projectId": project_id,
                "tool": bcfg.get("tool", "PINHOLE"),
                "userPaygateTier": bcfg.get("user_paygate_tier", "PAYGATE_TIER_TWO"),
                "sessionId": self._session_id,
                "recaptchaContext": {
                    "token": str(captcha_token),
                    "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                },
            },
            "requests": requests_list,
            "useV2ModelConfig": True,
        }

        if self._browser_runtime_enabled():
            ops = self._execute_video_payload_in_browser(
                row, "I2V", self.I2V_URL, payload, cache_attr="_i2v_auth_variant"
            )
            if ops:
                return {
                    "op_name": ops[0]["operation"]["name"],
                    "scene_id": scene_ids_local[0],
                    "ops": ops,
                }
            if not bcfg.get("veo_browser_runtime_fallback_requests", True):
                return None

        har_headers = self._build_har_headers()
        variants = self._build_bearer_variants()

        ops = self._try_auth_variants(
            row,
            self.I2V_URL,
            payload,
            har_headers,
            variants,
            tag="I2V",
            cache_attr="_i2v_auth_variant",
            refresh_on_401=True,
        )

        if not ops:
            return None

        return {
            "op_name": ops[0]["operation"]["name"],
            "scene_id": scene_ids_local[0],
            "ops": ops,
        }

    def create_video_from_image(
        self, row, prompt, project_id, captcha_token, start_image_media_id, **kwargs
    ):
        return self.create_video_i2v(
            row, prompt, project_id, captcha_token, start_image_media_id, **kwargs
        )

    START_END_URL = "https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoStartAndEndImage"

    def create_video_start_end_image(
        self,
        row,
        prompt,
        project_id,
        captcha_token,
        start_image_media_id,
        end_image_media_id,
        aspect="VIDEO_ASPECT_RATIO_PORTRAIT",
        seed=None,
        count=1,
        model_key=None,
    ):
        """
        Create video from start + end frame using the dedicated
        batchAsyncGenerateVideoStartAndEndImage endpoint (from HAR analysis).
        """
        if not self.access_token:
            self.get_session_token()
            if not self.access_token:
                self._last_error_detail = "[I2V-SE] Access token rỗng — get_session_token() thất bại."
                return None

        aspect = self._normalize_aspect(aspect)
        is_portrait = aspect == "VIDEO_ASPECT_RATIO_PORTRAIT"

        if not model_key:
            # Google Flow sử dụng model start/end mới theo repo mẫu.
            model_key = "veo_3_1_i2v_s_fast_ultra"

        _safe_print(
            f"[Row {row}] [I2V-SE] Model: {model_key}, start={start_image_media_id}, end={end_image_media_id}"
        )

        seeds, _ = self._generate_seeds_and_scenes(count, seed)
        batch_id = str(uuid.uuid4())

        requests_list = []
        for i in range(count):
            requests_list.append(
                {
                    "aspectRatio": aspect,
                    "seed": seeds[i],
                    "textInput": {"structuredPrompt": {"parts": [{"text": prompt}]}},
                    "videoModelKey": model_key,
                    "metadata": {},
                    "startImage": {
                        "mediaId": start_image_media_id,
                        "cropCoordinates": {"top": 0, "left": 0, "bottom": 1, "right": 1},
                    },
                    "endImage": {
                        "mediaId": end_image_media_id,
                        "cropCoordinates": {"top": 0, "left": 0, "bottom": 1, "right": 1},
                    },
                }
            )

        payload = {
            "mediaGenerationContext": {
                "batchId": batch_id,
                "audioFailurePreference": "BLOCK_SILENCED_VIDEOS",
            },
            "clientContext": {
                "projectId": project_id,
                "tool": bcfg.get("tool", "PINHOLE"),
                "userPaygateTier": bcfg.get("user_paygate_tier", "PAYGATE_TIER_TWO"),
                "sessionId": self._session_id,
                "recaptchaContext": {
                    "token": str(captcha_token),
                    "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                },
            },
            "requests": requests_list,
            "useV2ModelConfig": True,
        }

        START_END_URL = "https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoStartAndEndImage"
        if self._browser_runtime_enabled():
            ops = self._execute_video_payload_in_browser(
                row, "I2V-SE", START_END_URL, payload, cache_attr="_i2v_se_auth_variant"
            )
            if ops:
                return {
                    "op_name": ops[0]["operation"]["name"],
                    "scene_id": batch_id,
                    "ops": ops,
                }
            if not bcfg.get("veo_browser_runtime_fallback_requests", True):
                return None

        har_headers = self._build_har_headers()
        variants = self._build_bearer_variants()


        ops = self._try_auth_variants(
            row,
            START_END_URL,
            payload,
            har_headers,
            variants,
            tag="I2V-SE",
            cache_attr="_i2v_se_auth_variant",
            refresh_on_401=True,
        )

        if not ops:
            return None

        return {
            "op_name": ops[0]["operation"]["name"],
            "scene_id": batch_id,
            "ops": ops,
        }

    def upscale_video(
        self,
        row,
        project_id,
        input_media_generation_id,
        model="veo_3_1_upsampler_1080p",
        aspect="16:9",
        captcha_token=None,
    ):
        if not self.access_token:
            self.get_session_token()
            if not self.access_token:
                return None

        aspect = self._normalize_aspect(aspect)

        has_valid_captcha = bool(captcha_token and len(str(captcha_token)) >= 100)
        upscale_scene_id = str(uuid.uuid4())

        client_context = {
            "projectId": project_id,
            "tool": bcfg.get("tool", "PINHOLE"),
            "userPaygateTier": bcfg.get("user_paygate_tier", "PAYGATE_TIER_TWO"),
            "sessionId": self._session_id,
        }
        if has_valid_captcha:
            client_context["recaptchaContext"] = {
                "token": str(captcha_token),
                "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
            }

        payload = {
            "requests": [
                {
                    "aspectRatio": aspect,
                    "resolution": "VIDEO_RESOLUTION_1080P",
                    "seed": random.randint(10000, 99999),
                    "videoInput": {"mediaId": input_media_generation_id},
                    "videoModelKey": model,
                    "metadata": {"sceneId": upscale_scene_id},
                }
            ],
            "clientContext": client_context,
        }

        if self._browser_runtime_enabled():
            ops = self._execute_video_payload_in_browser(
                row, "Upscale", self.UPSCALE_URL, payload, cache_attr="_upscale_auth_variant"
            )
            if ops:
                op = ops[0]
                upscaled_media_id = op.get("mediaGenerationId") or op.get("operation", {}).get("name")
                return {
                    "upscaled_media_id": upscaled_media_id,
                    "op_name": upscaled_media_id,
                    "scene_id": upscale_scene_id,
                    "status": op.get("status"),
                    "ops": ops,
                    "raw_response": {"operations": ops},
                }
            if not bcfg.get("veo_browser_runtime_fallback_requests", True):
                return None

        if not has_valid_captcha:
            _safe_print(
                f"[Row {row}] [Upscale] BrowserRuntime failed and no valid captcha for direct fallback "
                f"(len={len(str(captcha_token)) if captcha_token else 0})"
            )
            return None

        headers = self._build_har_headers()
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        # Không set cookie khi tạo video/upscale (chỉ duy nhất Bearer token)

        try:
            resp = self._cffi_post(
                self.UPSCALE_URL, headers, json.dumps(payload), proxy=self.proxy
            )
        except Exception as e:
            _safe_print(f"[Row {row}] [Upscale] Exception: {e}")
            return None

        status = resp.status_code
        text = resp.text or ""

        if status != 200:
            _safe_print(f"[Row {row}] [Upscale] Failed. Status: {status}")
            _safe_print(f"[Row {row}] [Upscale] Response: {text[:500]}")
            return None

        try:
            jr = resp.json()
        except Exception:
            _safe_print(f"[Row {row}] [Upscale] Response not JSON")
            return None

        ops = jr.get("operations", [])
        if not ops:
            _safe_print(f"[Row {row}] [Upscale] Response missing operations: {jr}")
            return None

        op = ops[0]
        upscaled_media_id = op.get("mediaGenerationId") or op.get("operation", {}).get(
            "name"
        )

        if not upscaled_media_id:
            _safe_print(f"[Row {row}] [Upscale] Response missing mediaGenerationId: {jr}")
            return None

        _safe_print(f"[Row {row}] [Upscale] Success. MediaId: {upscaled_media_id}")
        return {
            "upscaled_media_id": upscaled_media_id,
            "op_name": upscaled_media_id,
            "scene_id": upscale_scene_id,
            "status": op.get("status"),
            "ops": ops,
            "raw_response": jr,
        }

    def check_status_batch(
        self, row, op_name, scene_id, current_status="MEDIA_GENERATION_STATUS_PENDING"
    ):
        headers = {
            "Content-Type": "application/json",
            "User-Agent": self.base_headers.get("User-Agent"),
            "Referer": self.base_headers.get("Referer"),
            "Origin": self.base_headers.get("Origin"),
            "x-browser-channel": self.base_headers.get("x-browser-channel"),
            "x-browser-validation": self.base_headers.get("x-browser-validation"),
            "x-client-data": self.base_headers.get("x-client-data"),
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        # Không set cookie khi poll status (chỉ duy nhất Bearer token)
        headers = {k: v for k, v in headers.items() if v}

        payload = {
            "operations": [
                {
                    "operation": {"name": op_name},
                    "sceneId": scene_id,
                    "status": current_status,
                }
            ]
        }
        try:
            resp = self._cffi_post(
                self.POLL_URL, headers, json.dumps(payload), timeout=10, proxy=self.proxy
            )
            if resp.status_code == 401:
                _safe_print("[Poll] Token expired (401), refreshing...")
                self.get_session_token()
                if self.access_token:
                    headers["Authorization"] = f"Bearer {self.access_token}"
                    resp = self._cffi_post(
                        self.POLL_URL, headers, json.dumps(payload), timeout=10, proxy=self.proxy
                    )
            return resp.json()
        except Exception as e:
            _safe_print(f"[Poll] Exception during check_status_batch: {e}")
            return None

    def get_media_download_url(self, media_name: str, no_proxy: bool = False) -> "str | None":
        """Lấy URL tải 720p trực tiếp qua labs.google TRPC API.

        Dùng khi polling op_name timeout nhưng video đã được generate xong ở backend.
        API: GET https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name={media_name}

        Args:
            media_name: tên media (op_name) cần tải
            no_proxy: nếu True, bỏ qua self.proxy (tải trực tiếp không qua proxy)

        Returns: video URL (redirect target) hoặc None
        """
        if not media_name:
            return None

        try:
            import requests as _req

            url = f"https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name={media_name}"

            headers = {
                "accept": "*/*",
                "referer": self._build_referer(),
                "user-agent": self.base_headers.get("User-Agent"),
                "sec-ch-ua": self.base_headers.get("sec-ch-ua",
                    '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"'),
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "Cookie": self.cookie,
            }
            headers = {k: v for k, v in headers.items() if v}

            _use_proxy = None
            _proxies = None
            resp = _req.get(
                url, headers=headers,
                proxies=None, timeout=15,
                allow_redirects=False,
                verify=False,
            )

            _filelog(
                f"[MediaRedirect] name={media_name[:20]}... "
                f"status={resp.status_code}"
            )

            # API trả redirect (302/307) hoặc JSON với URL
            if resp.status_code in (302, 307):
                redirect_url = resp.headers.get("Location")
                if redirect_url:
                    _filelog(f"[MediaRedirect] ✅ Got redirect URL: {redirect_url[:80]}...")
                    return redirect_url

            # Nếu trả 200 JSON
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    # TRPC response format: {"result": {"data": "https://..."}}
                    if isinstance(data, dict):
                        result = data.get("result", {})
                        if isinstance(result, dict):
                            video_url = result.get("data")
                            if video_url and isinstance(video_url, str) and video_url.startswith("http"):
                                _filelog(f"[MediaRedirect] ✅ Got URL from JSON: {video_url[:80]}...")
                                return video_url
                        
                        # Fallback keys
                        direct = data.get("url") or data.get("data") or data.get("videoUrl")
                        if direct and isinstance(direct, str) and direct.startswith("http"):
                            _filelog(f"[MediaRedirect] ✅ Got direct URL: {direct[:80]}...")
                            return direct
                except Exception:
                    pass

                # Body là plain URL
                body = resp.text.strip()
                if body.startswith("http"):
                    _filelog(f"[MediaRedirect] ✅ Got URL from body: {body[:80]}...")
                    return body

            _filelog(
                f"[MediaRedirect] ❌ Không lấy được URL. "
                f"status={resp.status_code} body={resp.text[:200]}"
            )
            return None

        except Exception as e:
            _filelog(f"[MediaRedirect] ❌ Error: {e}")
            return None

    def upload_user_image(
        self,
        image_base64,
        mime="image/jpeg",
        aspect="IMAGE_ASPECT_RATIO_LANDSCAPE",
        project_id=None,
        file_name="upload.jpg",
    ):
        try:
            import base64
            import io
            from PIL import Image

            img_data = base64.b64decode(image_base64)
            img = Image.open(io.BytesIO(img_data))

            w, h = img.size
            
            buf = io.BytesIO()
            if img.mode in ("RGBA", "P") and mime in ("image/jpeg", "image/jpg"):
                img = img.convert("RGB")

            save_format = "PNG" if "png" in mime.lower() else "JPEG"
            img.save(buf, format=save_format, quality=100)
            image_base64 = base64.b64encode(buf.getvalue()).decode("utf-8")
            _safe_print(f"[Upload] Prepared image {w}x{h} (quality=100) without cropping")
        except Exception as crop_err:
            _safe_print(f"[Upload] Prepare failed, using original: {crop_err}")

        UPLOAD_URL_V2 = "https://aisandbox-pa.googleapis.com/v1/flow/uploadImage"

        # ── Diagnostic helpers: short hash cho token/cookie để log không lộ secret ──
        import hashlib as _hl
        def _hsh(s: str, n: int = 10) -> str:
            """Hash 10 ký tự đầu MD5 — nhất quán cùng input giống nhau, giúp phát hiện
            token/cookie bị thay đổi giữa các lần retry."""
            if not s:
                return "EMPTY"
            try:
                return _hl.md5(s.encode("utf-8", errors="replace")).hexdigest()[:n]
            except Exception:
                return "HASH_ERR"

        def _email_hint(cookie: str) -> str:
            """Extract email hint từ cookie nếu có (để xác định cookie thuộc account nào)."""
            try:
                if not cookie:
                    return "?"
                # Labs session cookie có dạng __Secure-next-auth.session-token=<JWT>
                # JWT payload có thể chứa email, nhưng encrypted. Bỏ qua để đơn giản.
                return f"cookie_hash={_hsh(cookie, 8)}"
            except Exception:
                return "?"

        _tok = self.access_token or ""
        _ck = self.cookie or ""
        _account_label = getattr(self, "_account_label", None) or "?"
        # Diag state — caller có thể đọc self._last_upload_diag sau khi call
        self._last_upload_diag = {
            "url": UPLOAD_URL_V2,
            "account_label": _account_label,
            "token_hash": _hsh(_tok),
            "token_prefix": _tok[:20],
            "token_length": len(_tok),
            "cookie_hash": _hsh(_ck),
            "cookie_length": len(_ck),
            "proxy": self.proxy,
            "project_id": project_id,
            "attempts": [],
        }

        # ── Upload endpoint: chỉ gửi Bearer, KHÔNG gửi cookie ──
        # Cookie labs.google (__Secure-next-auth.session-token) là NextAuth JWT,
        # KHÔNG phải Google SSO cookie (SAPISID/__Secure-3PSID). Khi gửi kèm Bearer
        # token tới aisandbox-pa.googleapis.com, Google đọc cookie → thấy không hợp
        # lệ → reject cả request với 401 invalid_token dù Bearer OK.
        #
        # Trước đây headers hardcode chỉ 9 field, thiếu `sec-fetch-site: cross-site`,
        # `sec-ch-ua-*`, `accept`, `accept-language`, `priority`, `x-browser-copyright`.
        # Google có thể dùng các header này để verify request là từ browser thật.
        # Imagen upsample + T2V/I2V đều dùng `_build_har_headers()` đầy đủ → work.
        # → Fix: dùng cùng pattern.
        headers = self._build_har_headers()
        headers["authorization"] = f"Bearer {self.access_token}"
        # Không set cookie — upload endpoint strict cross-site, không chấp nhận cookie
        # từ labs.google vì Google coi là không hợp lệ.
        headers = {k: v for k, v in headers.items() if v}

        client_context = {
            "sessionId": f";{int(time.time() * 1000)}",
            "tool": "PINHOLE",
        }
        if project_id:
            client_context["projectId"] = project_id

        payload = {
            "clientContext": client_context,
            "imageBytes": image_base64,
        }

        # ── Pre-request log: đủ info để đối chiếu nếu 401 ──
        # Lưu ý: endpoint uploadImage KHÔNG gửi cookie (xem comment phía trên).
        # cookie_hash vẫn log để biết DB có cookie hợp lệ không (dùng để mint token).
        _safe_print(
            f"[Upload] → POST {UPLOAD_URL_V2} "
            f"account={_account_label} token_hash={_hsh(_tok)} token_len={len(_tok)} "
            f"cookie_in_db={_hsh(_ck)} (NOT sent) "
            f"proxy={self.proxy or 'DIRECT'} project={str(project_id)[:12] if project_id else '?'}"
        )
        _safe_print(f"[Upload] Header keys (cookie intentionally omitted): {list(headers.keys())}")

        proxies = None

        try:
            import json as _json
            resp = requests.post(
                UPLOAD_URL_V2, headers=headers, data=_json.dumps(payload), timeout=60, proxies=None, verify=False
            )

            # Record attempt #1
            _att1 = {
                "try": 1,
                "status": resp.status_code,
                "token_hash": _hsh(_tok),
                "response_headers": dict(resp.headers) if hasattr(resp, "headers") else {},
                "body_preview": (resp.text or "")[:600],
            }
            self._last_upload_diag["attempts"].append(_att1)

            if resp.status_code == 401:
                # Token hết hạn → tự refresh rồi retry 1 lần
                _old_tok_hash = _hsh(_tok)
                _resp_hdrs = dict(resp.headers) if hasattr(resp, "headers") else {}
                _safe_print(
                    f"[Upload] ❌ 401 on first try — account={_account_label} "
                    f"token_hash={_old_tok_hash}"
                )
                # Log response header đặc trưng (WWW-Authenticate, x-*-scope, etc)
                _auth_hint = _resp_hdrs.get("WWW-Authenticate") or _resp_hdrs.get("www-authenticate")
                if _auth_hint:
                    _safe_print(f"[Upload] 401 response WWW-Authenticate: {_auth_hint[:300]}")
                # Log body đầy đủ lần 401 (tìm error.code + error.status + details)
                _safe_print(f"[Upload] 401 response body: {(resp.text or '')[:600]}")

                _safe_print(f"[Upload] 401 → refreshing token and retrying...")
                self.get_session_token()
                _new_tok = self.access_token or ""
                _new_tok_hash = _hsh(_new_tok)
                _tok_changed = (_old_tok_hash != _new_tok_hash)
                _safe_print(
                    f"[Upload] After refresh: token_hash {_old_tok_hash} → {_new_tok_hash} "
                    f"{'(CHANGED ✓)' if _tok_changed else '(SAME ✗ session returned cached token)'}"
                )

                if self.access_token:
                    headers["authorization"] = f"Bearer {self.access_token}"
                    _safe_print(f"[Upload] Retry with new token: {self.access_token[:40]}...")
                    try:
                        resp2 = requests.post(
                            UPLOAD_URL_V2, headers=headers, data=_json.dumps(payload),
                            timeout=60, proxies=None, verify=False
                        )
                        _att2 = {
                            "try": 2,
                            "status": resp2.status_code,
                            "token_hash": _new_tok_hash,
                            "token_changed_from_try1": _tok_changed,
                            "response_headers": dict(resp2.headers) if hasattr(resp2, "headers") else {},
                            "body_preview": (resp2.text or "")[:600],
                        }
                        self._last_upload_diag["attempts"].append(_att2)

                        if resp2.status_code == 200:
                            resp = resp2  # success → tiếp tục xử lý bên dưới
                        else:
                            _r2_hdrs = dict(resp2.headers) if hasattr(resp2, "headers") else {}
                            _auth_hint2 = _r2_hdrs.get("WWW-Authenticate") or _r2_hdrs.get("www-authenticate")
                            _safe_print(
                                f"[Upload] ❌ Retry still failed: status={resp2.status_code} "
                                f"token_changed={_tok_changed} account={_account_label}"
                            )
                            if _auth_hint2:
                                _safe_print(f"[Upload] 401 retry WWW-Authenticate: {_auth_hint2[:300]}")
                            _safe_print(f"[Upload] 401 retry body: {(resp2.text or '')[:600]}")
                            # Diag summary — phân loại pattern 401:
                            if resp2.status_code == 401 and not _tok_changed:
                                _safe_print(
                                    f"[Upload] 🔍 DIAG: SAME token, both calls 401 → "
                                    f"khả năng proxy/cookie mismatch hoặc scope thiếu"
                                )
                            elif resp2.status_code == 401 and _tok_changed:
                                _safe_print(
                                    f"[Upload] 🔍 DIAG: NEW token vẫn 401 → "
                                    f"cookie thực sự hết hạn hoặc account bị Google reject"
                                )
                            self._last_upload_status = resp2.status_code
                            return None
                    except Exception as retry_err:
                        _safe_print(f"[Upload] Retry error: {retry_err}")
                        self._last_upload_diag["attempts"].append({
                            "try": 2, "exception": str(retry_err)[:300]
                        })
                        self._last_upload_status = 401
                        return None
                else:
                    _safe_print(
                        f"[Upload] ❌ Token refresh failed — get_session_token() trả rỗng. "
                        f"Cookie có thể đã invalid. cookie_hash={_hsh(_ck)}"
                    )
                    self._last_upload_diag["attempts"].append({
                        "try": 2, "error": "session_token_refresh_returned_empty"
                    })
                    self._last_upload_status = 401
                    return None

            if resp.status_code != 200:
                _safe_print(
                    f"[Upload] ❌ Failed: status={resp.status_code} account={_account_label}"
                )
                _safe_print(f"[Upload] Response body: {(resp.text or '')[:600]}")
                self._last_upload_status = resp.status_code
                return None

            jr = resp.json()
            image_id = None
            if isinstance(jr, dict):
                media_obj = jr.get("media", {})
                if "name" in media_obj:
                    image_id = media_obj["name"]
                else:
                    image_id = (
                        jr.get("mediaGenerationId", {}).get("mediaGenerationId")
                        or jr.get("imageId")
                        or jr.get("id")
                    )

            _safe_print(
                f"[Upload] Success. Image ID: {image_id[:40] if image_id else 'NONE'}..."
            )
            return image_id
        except Exception as e:
            _safe_print(f"[Upload] Error: {e}")
            return None

    def upload_image_from_path(
        self, image_path, aspect="IMAGE_ASPECT_RATIO_LANDSCAPE", project_id=None
    ):
        import base64
        import mimetypes
        import os
        import hashlib

        try:
            with open(image_path, "rb") as f:
                img_bytes = f.read()

            # ── Cache check: tái sử dụng nếu cùng ảnh + cùng project ──
            file_hash = hashlib.md5(img_bytes).hexdigest()
            cache_key = f"{file_hash}_{project_id or 'default'}"
            cached = _IMAGE_UPLOAD_CACHE.get(cache_key)
            if cached:
                age = time.time() - cached["ts"]
                if age < _IMAGE_CACHE_TTL:
                    _safe_print(
                        f"[Upload] ♻️ Cache hit: {os.path.basename(image_path)} → "
                        f"reuse media_id={cached['media_id'][:30]}... (age={int(age)}s)"
                    )
                    return cached["media_id"]
                else:
                    # Cache hết hạn
                    del _IMAGE_UPLOAD_CACHE[cache_key]

            img_b64 = base64.b64encode(img_bytes).decode("utf-8")
            mime_type = mimetypes.guess_type(image_path)[0] or "image/jpeg"
            file_name = os.path.basename(image_path)
            media_id = self.upload_user_image(
                img_b64,
                mime=mime_type,
                aspect=aspect,
                project_id=project_id,
                file_name=file_name,
            )

            # ── Cache kết quả upload thành công ──
            if media_id:
                # Dọn cache cũ nếu đầy
                if len(_IMAGE_UPLOAD_CACHE) >= _IMAGE_CACHE_MAX:
                    # Xóa entry cũ nhất
                    oldest_key = min(_IMAGE_UPLOAD_CACHE, key=lambda k: _IMAGE_UPLOAD_CACHE[k]["ts"])
                    del _IMAGE_UPLOAD_CACHE[oldest_key]
                _IMAGE_UPLOAD_CACHE[cache_key] = {"media_id": media_id, "ts": time.time()}
                _safe_print(
                    f"[Upload] 💾 Cached: {os.path.basename(image_path)} → {media_id[:30]}..."
                )

            return media_id
        except Exception as e:
            _safe_print(f"[Upload] Error reading file: {e}")
            return None


    def refresh_flow_cookie(self):
        _safe_print("[Auth] Cookie may be expired. To refresh:")
        _safe_print("  1) Open https://labs.google and sign in")
        _safe_print("  2) Copy __Secure-next-auth.session-token from Cookies")
        return False
