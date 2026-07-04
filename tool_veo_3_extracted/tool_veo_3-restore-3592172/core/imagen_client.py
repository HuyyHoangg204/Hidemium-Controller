import base64
import io
import json
import os
import re
import time
import uuid
from typing import Dict, List, Optional
import random
import requests
# Tắt warning verify=False của urllib3
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- 1) BẢNG MAP MODEL CŨ CỦA BẠN (Dữ phòng) ---
from core import config as cfg, browser_config as bcfg
from core.http_helpers import request_with_retries, HTTPError


import logging as _logging

# Dùng logger thay vì print() — logger ghi vào file, KHÔNG đụng console stdout
# → Tránh OSError: [Errno 22] Invalid argument trên VPS console encoding
_logger = _logging.getLogger(__name__)


def _safe_print(*args, **kwargs):
    """Redirect to logger.info — never touches stdout, prevents OSError on VPS."""
    try:
        msg = " ".join(str(a) for a in args)
        _logger.info(msg)
    except Exception:
        pass  # silent fallback




class ImagenPermissionError(Exception):
    """Raised khi Imagen API trả về 403 PERMISSION_DENIED (reCAPTCHA / IP blocked).
    Khác với cookie hết hạn (401) — cookie vẫn valid nhưng Imagen từ chối request.
    """
    def __init__(self, status_code, message):
        self.status_code = status_code
        self.api_message = message
        super().__init__(f"Imagen {status_code}: {message}")


class ImagenClient:

    # ── Class-level: giữ sessionId cố định per cookie (account) ──
    # Browser thật chỉ dùng 1 sessionId cho cả session.
    # Dict này đảm bảo mỗi cookie luôn nhận lại cùng sessionId
    # kể cả khi tạo ImagenClient mới cho mỗi task.
    _cookie_session_ids: Dict[str, str] = {}

    GENERATE_URL = (
        "https://aisandbox-pa.googleapis.com/v1/projects/{project_id}"
        "/flowMedia:batchGenerateImages"
    )
    UPSAMPLE_URL = "https://aisandbox-pa.googleapis.com/v1/flow/upsampleImage"
    MEDIA_URL = "https://aisandbox-pa.googleapis.com/v1/media/{media_id}"
    SESSION_URL = "https://labs.google/fx/api/auth/session"
    API_KEY = "AIzaSyBtrm0o5ab1c-Ec8ZuLcGt3oJAA5VWt3pY"

    RESOLUTION_MAP = {
        # "1K" đã bị Google remove (API trả 400 INVALID_ARGUMENT)
        "2K": "UPSAMPLE_IMAGE_RESOLUTION_2K",
        "4K": "UPSAMPLE_IMAGE_RESOLUTION_4K",
    }

    ASPECT_MAP = {
        "landscape": "IMAGE_ASPECT_RATIO_LANDSCAPE",
        "16:9": "IMAGE_ASPECT_RATIO_LANDSCAPE",
        "16_9": "IMAGE_ASPECT_RATIO_LANDSCAPE",
        "portrait": "IMAGE_ASPECT_RATIO_PORTRAIT",
        "9:16": "IMAGE_ASPECT_RATIO_PORTRAIT",
        "9_16": "IMAGE_ASPECT_RATIO_PORTRAIT",
        "square": "IMAGE_ASPECT_RATIO_SQUARE",
        "1:1": "IMAGE_ASPECT_RATIO_SQUARE",
    }

    def __init__(self, cookie, proxy=None, access_token=None):
        normalized = cookie
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
            else:
                cookie_header = c
        else:
            cookie_header = cookie

        self.cookie = cookie_header
        self.proxy_url = None
        if proxy:
            _safe_print("[Proxy] ImagenClient ignoring proxy parameter — DIRECT mode forced")

        ua = bcfg.get(
            "user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        )

        bval = bcfg.get("x_browser_validation")
        
        # [KEY] Khởi tạo Session duy nhất để dùng xuyên suốt Generate -> Upsample
        # Để curl_requests tự động quản lý Header và TLS thông qua impersonate
        from curl_cffi import requests as curl_requests
        self.session = curl_requests.Session(
            impersonate="chrome110",
            proxy=None,
            verify=False
        )
        # Chỉ để lại các header cơ bản, còn lại để AUTO
        self.base_headers = {
            "Referer": "https://labs.google/",
            "Origin": "https://labs.google",
            "Content-Type": "text/plain;charset=UTF-8",
        }
        self.session.headers.update(self.base_headers)
        
        # Thêm lại session_id bị thiếu
        import time
        self._session_id = f";{int(time.time() * 1000)}"
        self._auth_variant = None
        self._last_error_detail = None

        if not bval:
            try:
                from generate_bval import generate_validation_header

                bval = generate_validation_header(ua, "windows")
            except Exception:
                pass

        self._auth_variant = None
        self._last_error_detail = None
        self._proxy = None
        self._proxies = None
        self.proxy = None # Proxy disabled: media runtime always runs DIRECT


        # sessionId cố định per cookie (account) — giống browser giữ 1 sessionId per page session
        # Dùng class-level dict để reuse sessionId khi tạo client mới cho cùng cookie
        _cookie_key = self.cookie[:60]  # dùng prefix của cookie làm key
        if _cookie_key not in ImagenClient._cookie_session_ids:
            ImagenClient._cookie_session_ids[_cookie_key] = f";{int(time.time() * 1000)}"
        self._session_id = ImagenClient._cookie_session_ids[_cookie_key]

        # Nếu được truyền access_token từ ngoài → dùng luôn, không gọi get_session_token()
        if access_token:
            self.access_token = access_token
            _safe_print(f"[Imagen][Auth] Using provided token: {access_token[:20]}...")
        else:
            self.access_token = None
            self.get_session_token()

    @property
    def proxy(self):
        return self._proxy

    @proxy.setter
    def proxy(self, value):
        self._proxy = None
        self._proxies = None
        self.session.proxies = {}

    def get_session_token(self):
        try:
            _safe_print("[Imagen][Auth] Getting access token from session...")
            resp = None

            # Dung requests truc tiep (khong can curl_cffi TLS fingerprint)
            try:
                resp = requests.get(
                    self.SESSION_URL,
                    headers=self.base_headers,
                    proxies=None,
                    timeout=15,
                    verify=False,
                )
            except Exception as req_err:
                _safe_print(f"[Imagen][Auth] requests failed: {req_err}")
                return False

            if resp is None:
                _safe_print("[Imagen][Auth] ERROR: Cannot get access_token. Cookie expired?")
                return False

            if resp.status_code == 200:
                try:
                    data = resp.json()
                except Exception:
                    _safe_print("[Imagen][Auth] Cannot parse JSON from response")
                    return False
                self.access_token = data.get("access_token")
                if self.access_token:
                    # Log email để biết cookie này thuộc Google account nào
                    _email = (
                        data.get("user", {}).get("email")
                        or data.get("email")
                        or "unknown"
                    )
                    _safe_print(f"[Imagen][Auth] Token OK: {self.access_token[:20]}... | account={_email}")
                    return True
                _safe_print(f"[Imagen][Auth] No access_token in response: {data}")
                return False
            text = resp.text if hasattr(resp, "text") else ""
            _safe_print(f"[Imagen][Auth] Error: {resp.status_code}. Response: {text[:300]}")
            return False
        except Exception as e:
            _safe_print(f"[Imagen][Auth] Exception: {e}")
            return False


    @staticmethod
    def _normalize_aspect(aspect):
        mapping = ImagenClient.ASPECT_MAP
        if aspect in mapping:
            return mapping[aspect]
        if aspect and aspect.startswith("IMAGE_ASPECT_RATIO_"):
            return aspect
        return "IMAGE_ASPECT_RATIO_LANDSCAPE"

    def _build_har_headers(self):
        """Chỉ trả về các Header đặc thù của Google, phần còn lại để curl_cffi lo"""
        headers = {
            "content-type": "text/plain;charset=UTF-8",
            "origin": "https://labs.google",
            "referer": "https://labs.google/",
            "x-browser-channel": "stable",
            "x-browser-copyright": bcfg.get("x_browser_copyright", "Copyright 2026 Google LLC. All Rights Reserved."),
            "x-browser-validation": bcfg.get("x_browser_validation"),
            "x-browser-year": bcfg.get("x_browser_year", "2026"),
        }
        x_client = bcfg.get("x_client_data")
        if x_client:
            headers["x-client-data"] = x_client
            
        return {k: v for k, v in headers.items() if v is not None}



    def _build_client_context(self, captcha_token, project_id):
        ctx = {
            "projectId": project_id,
            "tool": "PINHOLE",
            "sessionId": self._session_id,
        }
        if captcha_token:
            ctx["recaptchaContext"] = {
                "token": captcha_token,
                "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
            }
        return ctx


    def _try_auth_variants(self, row, url, payload, headers, label="API", use_chrome_tls=True, bypass_proxy=False):
        """
        Thực hiện gọi API sử dụng Session đã được khởi tạo.
        """
        from curl_cffi import requests as curl_requests
        
        # Cập nhật proxy nếu cần (ví dụ bypass_proxy)
        if bypass_proxy:
            self.session.proxies = {}
        else:
            self.session.proxies = {"http": self.proxy_url, "https": self.proxy_url} if self.proxy_url else {}

        # Gửi request
        try:
            # Thêm token vào headers nếu có
            if self.access_token:
                headers["Authorization"] = f"Bearer {self.access_token}"

            resp = self.session.post(
                url,
                json=payload,
                headers=headers,
                timeout=120
            )
            
            if resp.status_code == 200:
                try:
                    return resp.json()
                except:
                    return resp.content
            
            # Xử lý lỗi
            err_body = resp.text[:500]
            self._last_error_detail = f"HTTP {resp.status_code}: {err_body}"
            _safe_print(f"[Row {row}] [{label}] ❌ Error {resp.status_code}: {err_body}")
            return None
            
        except Exception as e:
            self._last_error_detail = f"Exception: {str(e)}"
            _safe_print(f"[Row {row}] [{label}] ❌ Exception: {e}")
            return None

    def generate_image(
        self,
        row,
        prompt,
        project_id,
        captcha_token,
        aspect="IMAGE_ASPECT_RATIO_LANDSCAPE",
        model="NARWHAL",
        seed=None,
        count=1,
        image_media_ids=None,
    ):
        aspect = self._normalize_aspect(aspect)
        
        # Ưu tiên GEM_PIX_2 nếu không chỉ định rõ (Vì model này ổn định hơn theo log)
        if not model or model not in ["NARWHAL", "GEM_PIX_2"]:
            model = "GEM_PIX_2"
        if seed is None:
            seed = random.randint(100000, 999999)

        ctx = self._build_client_context(captcha_token, project_id)

        image_inputs = []
        if image_media_ids:
            for mid in image_media_ids:
                if mid:
                    image_inputs.append(
                        {"imageInputType": "IMAGE_INPUT_TYPE_REFERENCE", "name": mid}
                    )

        requests_list = []
        for _ in range(count):
            s = random.randint(100000, 999999) if count > 1 else seed
            requests_list.append(
                {
                    "clientContext": ctx,
                    "seed": s,
                    "imageModelName": model,
                    "imageAspectRatio": aspect,
                    "structuredPrompt": {"parts": [{"text": prompt}]},
                    "imageInputs": image_inputs,
                }
            )

        payload = {
            "clientContext": ctx,
            "mediaGenerationContext": {"batchId": str(uuid.uuid4())},
            "useNewMedia": True,
            "requests": requests_list,
        }

        url = self.GENERATE_URL.format(project_id=project_id)
        har_headers = self._build_har_headers()

        _safe_print(f"[Row {row}] [Imagen][Generate] Sending request for {count} image(s)...")
        _safe_print(
            f"[Row {row}] [Imagen][Generate] model={model}, aspect={aspect}, prompt={prompt[:80]!r}"
        )
        
        result = self._try_auth_variants(
            row, url, payload, har_headers, "Generate",
        )


        if result is None:
            return None

        if not isinstance(result, dict):
            _safe_print(f"[Row {row}] [Imagen][Generate] Unexpected response type")
            return None

        media_list = result.get("media", [])
        if not media_list:
            _safe_print(
                f"[Row {row}] [Imagen][Generate] No media in response: {str(result)[:300]}"
            )
            return None

        images = []
        for m in media_list:
            img = m.get("image", {}).get("generatedImage", {})
            images.append(
                {
                    "media_id": m.get("name"),
                    "fife_url": img.get("fifeUrl"),
                    "seed": img.get("seed"),
                    "aspect": img.get("aspectRatio"),
                    "prompt": img.get("prompt"),
                }
            )

        _safe_print(f"[Row {row}] [Imagen][Generate] Got {len(images)} image(s)")
        return images

    def prepare_generate_image(
        self, prompt, project_id, aspect="IMAGE_ASPECT_RATIO_LANDSCAPE",
        model="NARWHAL", seed=None, count=1, image_media_ids=None
    ):
        """Trả về (url, har_headers, payload_fn) để caller tự gọi fetch (ví dụ: browser_post_json).
        payload_fn(captcha_token) -> dict
        """
        aspect = self._normalize_aspect(aspect)
        model = "NARWHAL"
        if seed is None:
            seed = random.randint(100000, 999999)

        image_inputs = []
        if image_media_ids:
            for mid in image_media_ids:
                if mid:
                    image_inputs.append(
                        {"imageInputType": "IMAGE_INPUT_TYPE_REFERENCE", "name": mid}
                    )

        def _build_payload(captcha_token):
            ctx = self._build_client_context(captcha_token, project_id)
            requests_list = []
            for _ in range(count):
                s = random.randint(100000, 999999) if count > 1 else seed
                requests_list.append({
                    "clientContext": ctx,
                    "seed": s,
                    "imageModelName": model,
                    "imageAspectRatio": aspect,
                    "structuredPrompt": {"parts": [{"text": prompt}]},
                    "imageInputs": image_inputs,
                })
            return {
                "clientContext": ctx,
                "mediaGenerationContext": {"batchId": str(uuid.uuid4())},
                "useNewMedia": True,
                "requests": requests_list,
            }

        url = self.GENERATE_URL.format(project_id=project_id)
        har_headers = self._build_har_headers()
        # Thêm Authorization vào headers cho browser fetch
        if self.access_token:
            har_headers["authorization"] = f"Bearer {self.access_token}"
        # KHÔNG gửi cookie khi tạo ảnh — cross-site request chỉ dùng Bearer token
        # if self.cookie:
        #     har_headers["cookie"] = self.cookie
        return url, har_headers, _build_payload

    def upsample_image(
        self,
        row,
        media_id,
        resolution,
        project_id,
        captcha_token,
    ):
        # Track diagnostic cho caller (veo_service có thể đọc self._last_upsample_detail)
        self._last_upsample_detail = {
            "resolution": resolution,
            "project_id_preview": (str(project_id)[:12] + "...") if project_id else None,
            "media_id_in": (str(media_id)[:50] + "...") if media_id and len(str(media_id)) > 50 else str(media_id or ""),
            "captcha_len": len(captcha_token) if captcha_token else 0,
            "step": "init",
        }

        res_upper = resolution.upper() if isinstance(resolution, str) else str(resolution)
        res_key = self.RESOLUTION_MAP.get(res_upper)
        if not res_key:
            msg = (
                f"Resolution '{resolution}' không được hỗ trợ — RESOLUTION_MAP chỉ có "
                f"{list(self.RESOLUTION_MAP.keys())}"
            )
            _safe_print(f"[Row {row}] [Imagen][Upsample] {msg}")
            self._last_upsample_detail["step"] = "unsupported_resolution"
            self._last_upsample_detail["error"] = msg
            self._last_error_detail = f"[Upsample] {msg}"
            return None

        # ── Extract plain UUID từ media_id ──
        _original_media_id = media_id
        if media_id and "/" in media_id:
            media_id = media_id.rsplit("/", 1)[-1]
            _safe_print(
                f"[Row {row}] [Imagen][Upsample] Extracted UUID from resource path: "
                f"{_original_media_id[:50]}... → {media_id}"
            )
            self._last_upsample_detail["media_id_extracted"] = media_id

        ctx = self._build_client_context(captcha_token, project_id)
        ctx["userPaygateTier"] = bcfg.get("user_paygate_tier", "PAYGATE_TIER_TWO")
        payload = {
            "mediaId": media_id,
            "targetResolution": res_key,
            "clientContext": ctx,
        }

        # Headers ĐẦY ĐỦ giống browser thật — copy từ cURL Chrome 147 thực tế
        _upsample_headers = {
            "accept": "*/*",
            "accept-language": bcfg.get(
                "accept_language",
                "vi-VN,vi;q=0.9,fr-FR;q=0.8,fr;q=0.7,en-US;q=0.6,en;q=0.5",
            ),
            "Content-Type": "text/plain;charset=UTF-8",
            "origin": "https://labs.google",
            "priority": "u=1, i",
            "Referer": "https://labs.google/",
            "sec-ch-ua": bcfg.get(
                "sec_ch_ua",
                '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
            ),
            "sec-ch-ua-mobile": bcfg.get("sec_ch_ua_mobile", "?0"),
            "sec-ch-ua-platform": bcfg.get("sec_ch_ua_platform", '"Windows"'),
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
            "User-Agent": self.base_headers.get(
                "User-Agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            ),
            "x-browser-channel": "stable",
            "x-browser-copyright": "Copyright 2026 Google LLC. All Rights Reserved.",
            "x-browser-validation": bcfg.get("x_browser_validation", "B2gM+WTW2xHE15IAjh8nDoMc5x0="),
            "x-browser-year": "2026",
            "x-client-data": bcfg.get(
                "x_client_data",
                "CKmdygEIk6HLAQiFoM0BCOO2zwEImb/PAQjSwM8BCOLAzwEY+r/PAQ==",
            ),
        }
        _upsample_headers = {k: v for k, v in _upsample_headers.items() if v}


        _safe_print(
            f"[Row {row}] [Imagen][Upsample] → POST {self.UPSAMPLE_URL} "
            f"resolution={resolution} target={res_key} mediaId={media_id} "
            f"project={str(project_id)[:12]}... captcha_len={len(captcha_token) if captcha_token else 0}"
        )
        self._last_upsample_detail["step"] = "http_request"
        self._last_upsample_detail["target_resolution_enum"] = res_key
        self._last_error_detail = None  # reset trước khi gọi

        result = self._try_auth_variants(
            row, self.UPSAMPLE_URL, payload, _upsample_headers, f"Upsample-{resolution}",
        )



        if result is None:
            # _try_auth_variants đã set self._last_error_detail (HTTP status + body preview)
            detail = self._last_error_detail or "api_returned_none"
            _safe_print(
                f"[Row {row}] [Imagen][Upsample] ❌ FAILED ({resolution}) — "
                f"API không trả về result. Detail: {detail}"
            )
            self._last_upsample_detail["step"] = "http_failed"
            self._last_upsample_detail["error"] = detail
            return None

        encoded = None
        if isinstance(result, dict):
            encoded = result.get("encodedImage")
        elif isinstance(result, str):
            encoded = result

        if not encoded:
            # Log đầy đủ structure để biết response bị gì
            try:
                if isinstance(result, dict):
                    _keys = list(result.keys())
                    _preview = json.dumps(result, ensure_ascii=False)[:400]
                else:
                    _keys = type(result).__name__
                    _preview = str(result)[:400]
            except Exception:
                _keys = "<unknown>"
                _preview = "<unparseable>"
            msg = f"Response thiếu 'encodedImage'. keys={_keys}"
            _safe_print(
                f"[Row {row}] [Imagen][Upsample] ❌ {msg}. Preview: {_preview}"
            )
            self._last_upsample_detail["step"] = "no_encoded_image"
            self._last_upsample_detail["response_keys"] = _keys
            self._last_upsample_detail["response_preview"] = _preview[:200]
            self._last_error_detail = f"[Upsample-{resolution}] {msg}"
            return None

        _safe_print(
            f"[Row {row}] [Imagen][Upsample] encodedImage length={len(encoded)} "
            f"prefix={encoded[:12]!r} (JPEG=/9j PNG=iVBO WEBP=UklG)"
        )
        self._last_upsample_detail["encoded_length"] = len(encoded)
        self._last_upsample_detail["encoded_prefix"] = encoded[:12]

        try:
            image_bytes = base64.b64decode(encoded)
        except Exception as e:
            msg = f"Base64 decode error: {e}"
            _safe_print(f"[Row {row}] [Imagen][Upsample] ❌ {msg}")
            self._last_upsample_detail["step"] = "b64_decode_failed"
            self._last_upsample_detail["error"] = str(e)[:200]
            self._last_error_detail = f"[Upsample-{resolution}] {msg}"
            return None

        # Detect format từ magic bytes để log rõ
        _fmt = "unknown"
        if len(image_bytes) >= 4:
            if image_bytes[:3] == b"\xff\xd8\xff":
                _fmt = "JPEG"
            elif image_bytes[:4] == b"\x89PNG":
                _fmt = "PNG"
            elif image_bytes[:4] == b"RIFF":
                _fmt = "WEBP"

        _safe_print(
            f"[Row {row}] [Imagen][Upsample] ✅ SUCCESS {resolution} — "
            f"{len(image_bytes)} bytes ({_fmt})"
        )
        self._last_upsample_detail["step"] = "success"
        self._last_upsample_detail["bytes"] = len(image_bytes)
        self._last_upsample_detail["format"] = _fmt
        return image_bytes

    def download_image(self, row, media_id):
        url = self.MEDIA_URL.format(media_id=media_id)
        url += f"?key={self.API_KEY}&clientContext.tool=PINHOLE"

        har_headers = self._build_har_headers()
        har_headers.pop("content-type", None)

        _safe_print(f"[Row {row}] [Imagen][Download] Fetching original image...")

        try:
            resp = self._cffi_get(url, har_headers, proxies=None)
        except Exception as e:
            _safe_print(f"[Row {row}] [Imagen][Download] Request exception: {e}")
            return None

        if resp is None or getattr(resp, "status_code", None) != 200:
            status = getattr(resp, "status_code", "N/A") if resp else "N/A"
            _safe_print(f"[Row {row}] [Imagen][Download] Failed: {status}")
            return None

        try:
            data = resp.json()
        except Exception:
            _safe_print(f"[Row {row}] [Imagen][Download] Response not JSON")
            return None

        encoded = data.get("image", {}).get("encodedImage")
        if not encoded:
            _safe_print(f"[Row {row}] [Imagen][Download] No encodedImage in response")
            return None

        try:
            image_bytes = base64.b64decode(encoded)
        except Exception as e:
            _safe_print(f"[Row {row}] [Imagen][Download] Base64 decode error: {e}")
            return None

        _safe_print(
            f"[Row {row}] [Imagen][Download] Got original image ({len(image_bytes)} bytes)"
        )
        return image_bytes

    def download_from_fife(self, row, fife_url):
        _safe_print(f"[Row {row}] [Imagen][FifeDownload] Fetching from storage...")
        try:
            resp = requests.get(fife_url, timeout=30, proxies=None)
            if resp.status_code == 200:
                _safe_print(
                    f"[Row {row}] [Imagen][FifeDownload] OK ({len(resp.content)} bytes)"
                )
                return resp.content
            _safe_print(f"[Row {row}] [Imagen][FifeDownload] Failed: {resp.status_code}")
            return None
        except Exception as e:
            _safe_print(f"[Row {row}] [Imagen][FifeDownload] Exception: {e}")
            return None

    @staticmethod
    def save_image(image_bytes, output_path):
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(image_bytes)
        return output_path

    def upload_user_image(
        self,
        image_base64,
        mime="image/jpeg",
        aspect="IMAGE_ASPECT_RATIO_LANDSCAPE",
        project_id=None,
        file_name="upload.jpg",
    ):
        UPLOAD_URL_V2 = "https://aisandbox-pa.googleapis.com/v1/flow/uploadImage"

        headers = {
            "content-type": "text/plain;charset=UTF-8",
            "origin": "https://labs.google",
            "referer": "https://labs.google/",
            "user-agent": self.base_headers.get("User-Agent"),
            "x-browser-channel": "stable",
            "x-browser-copyright": bcfg.get("x_browser_copyright", "Copyright 2026 Google LLC. All Rights Reserved."),
            "x-browser-validation": bcfg.get("x_browser_validation"),
            "x-browser-year": bcfg.get("x_browser_year", "2026"),
        }
        x_client = bcfg.get("x_client_data")
        if x_client:
            headers["x-client-data"] = x_client
        
        headers = {k: v for k, v in headers.items() if v is not None}
        if self.access_token:
            headers["authorization"] = f"Bearer {self.access_token}"

        # KHÔNG gửi cookie khi tạo/upload ảnh — cross-site request chỉ dùng Bearer token
        # if self.cookie:
        #     headers["cookie"] = self.cookie
            
        headers = {k: v for k, v in headers.items() if v}

        client_context = {
            "sessionId": self._session_id,
            "tool": "PINHOLE",
        }
        if project_id:
            client_context["projectId"] = project_id

        payload = {
            "clientContext": client_context,
            "imageBytes": image_base64,
        }

        _safe_print(
            f"[ImagenUpload] Sending upload to project={str(project_id)[:30]} "
            f"token={self.access_token[:30]}..."
        )

        proxies = None

        try:
            # QUAN TRỌNG: dùng data=json.dumps() + content-type đã set trong headers
            # KHÔNG dùng json=payload vì requests sẽ ghi đè Content-Type thành
            # 'application/json' — làm mất header 'text/plain;charset=UTF-8'
            # mà Google Upload API yêu cầu (giống generate endpoint)
            import json as _json
            resp = requests.post(
                UPLOAD_URL_V2,
                headers=headers,
                data=_json.dumps(payload),
                timeout=30,
                proxies=None,
                verify=False,
            )

            if resp.status_code != 200:
                _safe_print(f"[ImagenUpload] Failed: status={resp.status_code}")
                _safe_print(f"[ImagenUpload] Response: {resp.text[:500]}")
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

            if image_id:
                _safe_print(
                    f"[ImagenUpload] ✅ Success — media_id={image_id[:60]}... "
                    f"(project={str(project_id)[:30]})"
                )
            else:
                _safe_print(
                    f"[ImagenUpload] ⚠️ Upload OK (200) nhưng không parse được media_id. "
                    f"Response keys: {list(jr.keys()) if isinstance(jr, dict) else type(jr)}"
                )
            return image_id
        except requests.exceptions.ProxyError as pe:
            _safe_print(f"[ImagenUpload] PROXY ERROR (proxy chết): {pe}")
            raise ConnectionError(f"Proxy connection failed: {pe}") from pe
        except requests.exceptions.ConnectionError as ce:
            _safe_print(f"[ImagenUpload] CONNECTION ERROR: {ce}")
            raise ConnectionError(f"Connection failed: {ce}") from ce
        except Exception as e:
            _safe_print(f"[ImagenUpload] Error: {e}")
            return None

    def upload_image_from_path(
        self, image_path, aspect="IMAGE_ASPECT_RATIO_LANDSCAPE", project_id=None
    ):
        import mimetypes
        import base64

        try:
            # Auto-resize ảnh lớn trước khi upload (giảm upload time đáng kể)
            MAX_SIDE = 1280  # px — đủ chất lượng cho reference image
            img_bytes = None
            mime_type = mimetypes.guess_type(image_path)[0] or "image/jpeg"
            file_name = os.path.basename(image_path)

            try:
                from PIL import Image as _PILImage
                import io as _io
                with _PILImage.open(image_path) as _img:
                    w, h = _img.size
                    if max(w, h) > MAX_SIDE:
                        ratio = MAX_SIDE / max(w, h)
                        new_w, new_h = int(w * ratio), int(h * ratio)
                        _img = _img.resize((new_w, new_h), _PILImage.LANCZOS)
                        print(f"[ImagenUpload] Resized {w}x{h} → {new_w}x{new_h}")
                    # Chuyển sang RGB nếu cần (loại bỏ alpha)
                    if _img.mode in ("RGBA", "P"):
                        _img = _img.convert("RGB")
                    buf = _io.BytesIO()
                    _img.save(buf, format="JPEG", quality=85)
                    img_bytes = buf.getvalue()
                    mime_type = "image/jpeg"
            except ImportError:
                # Pillow chưa cài → dùng file gốc
                print("[ImagenUpload] PIL not found, using original file (may be large)")
                with open(image_path, "rb") as f:
                    img_bytes = f.read()
            except Exception as _pe:
                print(f"[ImagenUpload] Resize error: {_pe}, using original file")
                with open(image_path, "rb") as f:
                    img_bytes = f.read()

            img_b64 = base64.b64encode(img_bytes).decode("utf-8")
            print(f"[ImagenUpload] Payload size: {len(img_b64)//1024}KB (file: {os.path.getsize(image_path)//1024}KB)")
            return self.upload_user_image(
                img_b64,
                mime=mime_type,
                aspect=aspect,
                project_id=project_id,
                file_name=file_name,
            )
        except ConnectionError:
            raise  # Proxy/network error phải propagate lên caller
        except Exception as e:
            _safe_print(f"[ImagenUpload] Error reading file: {e}")
            return None

