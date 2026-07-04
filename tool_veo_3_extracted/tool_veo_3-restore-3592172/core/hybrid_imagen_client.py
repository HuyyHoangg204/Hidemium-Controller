"""
hybrid_imagen_client.py — Drop-in replacement cho `core.imagen_client.ImagenClient`.

Logic:
  1. Constructor giữ nguyên signature ImagenClient + thêm `account_email` (optional).
  2. Mỗi public method (generate_image, upload_user_image, upsample_image, ...):
     - Nếu Chrome của account_email đã connect với bridge → gọi BrowserFlowClient
       (request đi qua tab labs.google → bypass UNUSUAL_ACTIVITY).
     - Browser fail → fallback ImagenClient (httpx + curl_cffi) như cũ.
  3. account_email = None → forward 100% sang ImagenClient (giữ behavior cũ).

Migration trong veo_service.py:
    cũ:  ImagenClient(cookie=..., proxy=..., access_token=...)
    mới: HybridImagenClient(cookie=..., proxy=..., access_token=...,
                            account_email=task.picked_account_name)
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class HybridImagenClient:
    """Wrapper auto-detect: browser fetch khi Chrome connected, httpx khi không."""

    def __init__(self, cookie: str, proxy: Optional[str] = None,
                 access_token: Optional[str] = None,
                 account_email: Optional[str] = None):
        # Lazy init: KHÔNG dựng ImagenClient httpx ngay. Nếu không truyền
        # access_token, ImagenClient.__init__ tự gọi get_session_token() qua
        # proxy → block ~180s khi proxy chết. Bridge online → ta không cần
        # httpx → bypass proxy hoàn toàn.
        self._cookie = cookie
        self._proxy = None
        if proxy:
            logger.info("[HybridImagen] Ignoring proxy parameter — DIRECT mode forced")
        self._init_access_token = access_token  # token user truyền (nếu có)
        self._account_email = account_email
        self._httpx_instance = None
        self._pending_access_token = access_token  # mirror để getter trả ra
        self._pending_last_error_detail = None
        self._browser = None
        if account_email:
            try:
                from core.browser_flow_client import BrowserFlowClient
                self._browser = BrowserFlowClient(account_email=account_email)
            except Exception as e:
                logger.warning(f"[Hybrid] BrowserFlowClient init fail cho {account_email}: {e}")
                self._browser = None

    # ── Lazy httpx backbone — chỉ tạo khi cần fallback ──
    @property
    def _httpx(self):
        """Lazy init ImagenClient. Truy cập đầu tiên trigger __init__ →
        get_session_token() qua proxy nếu chưa có token. Bridge online + TĐ1
        skip validate → property này KHÔNG bao giờ chạy."""
        if self._httpx_instance is None:
            from core.imagen_client import ImagenClient
            # Truyền pending_access_token (nếu user/Section 1.5 đã set sau init)
            # → ImagenClient bỏ qua get_session_token() trong __init__
            tok = self._pending_access_token or self._init_access_token
            self._httpx_instance = ImagenClient(
                cookie=self._cookie, proxy=None, access_token=tok,
            )
            if self._pending_last_error_detail is not None:
                self._httpx_instance._last_error_detail = self._pending_last_error_detail
        return self._httpx_instance

    # ── Forward attributes có sẵn của ImagenClient (lazy-aware) ──
    @property
    def access_token(self):
        if self._httpx_instance is None:
            return self._pending_access_token
        return self._httpx_instance.access_token

    @access_token.setter
    def access_token(self, v):
        if self._httpx_instance is None:
            self._pending_access_token = v
        else:
            self._httpx_instance.access_token = v

    @property
    def cookie(self):
        if self._httpx_instance is None:
            return self._cookie
        return self._httpx_instance.cookie

    @property
    def proxy(self):
        if self._httpx_instance is None:
            return self._proxy
        return self._httpx_instance.proxy

    @proxy.setter
    def proxy(self, v):
        self._proxy = None
        if self._httpx_instance is not None:
            self._httpx_instance.proxy = None

    @property
    def session(self):
        return self._httpx.session  # trigger lazy

    @property
    def _last_error_detail(self):
        br_err = getattr(self._browser, "_last_error_detail", None) if self._browser else None
        if br_err:
            return br_err
        if self._httpx_instance is None:
            return self._pending_last_error_detail
        return getattr(self._httpx_instance, "_last_error_detail", None)

    @property
    def _session_id(self):
        return self._httpx._session_id  # trigger lazy

    def __getattr__(self, name):
        """Forward tất cả attribute/method khác sang httpx client.
        __getattr__ chỉ chạy khi lookup thường fail → trigger lazy init."""
        return getattr(self._httpx, name)

    # ── Helper: Browser available? ──
    def _browser_available(self) -> bool:
        if not self._browser:
            return False
        try:
            return self._browser.is_connected()
        except Exception:
            return False

    # ── generate_image — bridge ưu tiên (đợi tối đa max_wait), fallback httpx ──
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
        from core.hybrid_helpers import call_with_bridge_priority

        def _browser_call():
            res = self._browser.generate_image(
                prompt=prompt, project_id=project_id, aspect=aspect,
                model=model, seed=seed, count=count,
                image_media_ids=image_media_ids, row=row,
            )
            if not isinstance(res, dict):
                return None
            media_list = res.get("media", [])
            if not media_list:
                return None
            images = []
            for m in media_list:
                img = m.get("image", {}).get("generatedImage", {})
                images.append({
                    "media_id": m.get("name"),
                    "fife_url": img.get("fifeUrl"),
                    "encoded_image": img.get("encodedImage"),
                })
            return images

        return call_with_bridge_priority(
            is_browser_available=self._browser_available,
            browser_fn=_browser_call,
            httpx_fn=lambda: self._httpx.generate_image(
                row=row, prompt=prompt, project_id=project_id,
                captcha_token=captcha_token, aspect=aspect, model=model,
                seed=seed, count=count, image_media_ids=image_media_ids,
            ),
            label=f"generate_image account={self._account_email}",
        )

    # ── upload_user_image — bridge ưu tiên, fallback httpx ──
    def upload_user_image(
        self,
        image_base64,
        mime="image/jpeg",
        aspect="IMAGE_ASPECT_RATIO_LANDSCAPE",
        project_id=None,
        file_name="upload.jpg",
    ):
        # Bridge cần project_id để biết tab labs.google nào → không có thì httpx luôn
        if not project_id or not self._browser:
            return self._httpx.upload_user_image(
                image_base64=image_base64, mime=mime, aspect=aspect,
                project_id=project_id, file_name=file_name,
            )

        from core.hybrid_helpers import call_with_bridge_priority

        return call_with_bridge_priority(
            is_browser_available=self._browser_available,
            browser_fn=lambda: self._browser.upload_user_image(
                image_base64=image_base64, project_id=project_id, mime=mime,
            ),
            httpx_fn=lambda: self._httpx.upload_user_image(
                image_base64=image_base64, mime=mime, aspect=aspect,
                project_id=project_id, file_name=file_name,
            ),
            label=f"upload_user_image account={self._account_email}",
        )

    # ── upload_image_from_path — pass-through (đọc file → base64 → upload) ──
    def upload_image_from_path(self, image_path, aspect="IMAGE_ASPECT_RATIO_LANDSCAPE",
                                project_id=None):
        # ImagenClient.upload_image_from_path tự đọc file rồi gọi upload_user_image
        # Override: đọc base64 trước, gọi self.upload_user_image (đã hybrid)
        import base64
        try:
            with open(image_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode("ascii")
        except Exception as e:
            logger.error(f"[Hybrid] Read image fail {image_path}: {e}")
            return None
        # Detect mime
        mime = "image/jpeg"
        ext = image_path.lower().split(".")[-1]
        if ext in ("png",):
            mime = "image/png"
        elif ext in ("webp",):
            mime = "image/webp"
        return self.upload_user_image(
            image_base64=img_b64, mime=mime, aspect=aspect,
            project_id=project_id, file_name=image_path.split("/")[-1],
        )
