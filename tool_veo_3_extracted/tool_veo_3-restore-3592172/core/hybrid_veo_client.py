"""
hybrid_veo_client.py — Drop-in replacement cho `core.veo_client.VeoClient`.

Logic giống `HybridImagenClient`: nếu Chrome của account_email đã connect với
bridge → gọi BrowserFlowClient (T2V/I2V/Frames qua tab labs.google) bypass
UNUSUAL_ACTIVITY. Browser fail → fallback VeoClient httpx.

Migration trong veo_service.py:
    cũ:  VeoClient(cookie, proxy=..)
    mới: HybridVeoClient(cookie, proxy=.., account_email=task.picked_account_name)
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class HybridVeoClient:
    def __init__(self, cookie: str, proxy: Optional[str] = None,
                 account_email: Optional[str] = None):
        # Lazy init: KHÔNG dựng VeoClient httpx ngay. VeoClient.__init__ tự gọi
        # get_session_token() qua proxy → proxy chết sẽ block ~180s ngay tại đây.
        # Khi bridge của account online, ta KHÔNG cần httpx → tránh đụng proxy
        # cho tới khi thực sự fallback xuống httpx_fn.
        self._cookie = cookie
        self._proxy = None
        if proxy:
            logger.info("[HybridVeo] Ignoring proxy parameter — DIRECT mode forced")
        self._account_email = account_email
        self._httpx_instance = None
        self._pending_access_token = None
        self._pending_last_error_detail = None
        self._browser = None
        if account_email:
            try:
                from core.browser_flow_client import BrowserFlowClient
                self._browser = BrowserFlowClient(account_email=account_email)
            except Exception as e:
                logger.warning(f"[HybridVeo] BrowserFlowClient init fail cho {account_email}: {e}")
                self._browser = None

    # ── Lazy httpx backbone — chỉ tạo khi cần fallback ──
    @property
    def _httpx(self):
        """Lazy init VeoClient. Truy cập đầu tiên trigger VeoClient.__init__ →
        get_session_token() qua proxy. Bridge online + TĐ1 skip validate →
        property này KHÔNG bao giờ chạy → 0 đụng proxy."""
        if self._httpx_instance is None:
            from core.veo_client import VeoClient
            self._httpx_instance = VeoClient(cookie=self._cookie, proxy=None)
            if self._pending_access_token is not None:
                self._httpx_instance.access_token = self._pending_access_token
            if self._pending_last_error_detail is not None:
                self._httpx_instance._last_error_detail = self._pending_last_error_detail
        return self._httpx_instance

    # ── Forward state attributes (lazy-aware) ──
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
    def base_headers(self):
        return self._httpx.base_headers  # trigger lazy

    @property
    def _session_id(self):
        return self._httpx._session_id  # trigger lazy

    @property
    def _last_error_detail(self):
        br_err = getattr(self._browser, "_last_error_detail", None) if self._browser else None
        if br_err:
            return br_err
        if self._httpx_instance is None:
            return self._pending_last_error_detail
        return getattr(self._httpx_instance, "_last_error_detail", None)

    @_last_error_detail.setter
    def _last_error_detail(self, v):
        if self._httpx_instance is None:
            self._pending_last_error_detail = v
        else:
            self._httpx_instance._last_error_detail = v

    def __getattr__(self, name):
        """Forward các attribute/method khác sang VeoClient (get_session_token,
        upscale_video, …). Truy cập trigger lazy init nếu chưa init."""
        return getattr(self._httpx, name)

    # ── Helper ──
    def _browser_available(self) -> bool:
        if not self._browser:
            return False
        try:
            return self._browser.is_connected()
        except Exception:
            return False

    # ── T2V / I2V / Frames — bridge ưu tiên, fallback httpx khi hết deadline ──

    def create_video_t2v(self, row, prompt, project_id, captcha_token, **kwargs):
        from core.hybrid_helpers import call_with_bridge_priority
        return call_with_bridge_priority(
            is_browser_available=self._browser_available,
            browser_fn=lambda: self._browser.create_video_t2v(
                row=row, prompt=prompt, project_id=project_id,
                captcha_token=captcha_token, **kwargs,
            ),
            httpx_fn=lambda: self._httpx.create_video_t2v(
                row=row, prompt=prompt, project_id=project_id,
                captcha_token=captcha_token, **kwargs,
            ),
            label=f"T2V account={self._account_email}",
        )

    def create_video(self, row, prompt, project_id, captcha_token, **kwargs):
        return self.create_video_t2v(row, prompt, project_id, captcha_token, **kwargs)

    def create_video_i2v(self, row, prompt, project_id, captcha_token,
                          start_image_media_id, **kwargs):
        from core.hybrid_helpers import call_with_bridge_priority
        return call_with_bridge_priority(
            is_browser_available=self._browser_available,
            browser_fn=lambda: self._browser.create_video_i2v(
                row=row, prompt=prompt, project_id=project_id,
                captcha_token=captcha_token,
                start_image_media_id=start_image_media_id, **kwargs,
            ),
            httpx_fn=lambda: self._httpx.create_video_i2v(
                row=row, prompt=prompt, project_id=project_id,
                captcha_token=captcha_token,
                start_image_media_id=start_image_media_id, **kwargs,
            ),
            label=f"I2V account={self._account_email}",
        )

    def create_video_from_image(self, row, prompt, project_id, captcha_token,
                                 start_image_media_id, **kwargs):
        return self.create_video_i2v(row, prompt, project_id, captcha_token,
                                       start_image_media_id, **kwargs)

    def create_video_start_end_image(self, row, prompt, project_id, captcha_token,
                                       start_image_media_id, end_image_media_id,
                                       **kwargs):
        from core.hybrid_helpers import call_with_bridge_priority
        return call_with_bridge_priority(
            is_browser_available=self._browser_available,
            browser_fn=lambda: self._browser.create_video_start_end_image(
                row=row, prompt=prompt, project_id=project_id,
                captcha_token=captcha_token,
                start_image_media_id=start_image_media_id,
                end_image_media_id=end_image_media_id, **kwargs,
            ),
            httpx_fn=lambda: self._httpx.create_video_start_end_image(
                row=row, prompt=prompt, project_id=project_id,
                captcha_token=captcha_token,
                start_image_media_id=start_image_media_id,
                end_image_media_id=end_image_media_id, **kwargs,
            ),
            label=f"I2V-SE account={self._account_email}",
        )

    # ── Upload — bridge cần project_id, không có → httpx luôn ──

    def upload_user_image(self, image_base64, mime="image/jpeg",
                           aspect="IMAGE_ASPECT_RATIO_LANDSCAPE",
                           project_id=None, file_name="upload.jpg"):
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

    def upload_image_from_path(self, image_path, aspect="IMAGE_ASPECT_RATIO_LANDSCAPE",
                                project_id=None):
        if not project_id or not self._browser:
            return self._httpx.upload_image_from_path(
                image_path=image_path, aspect=aspect, project_id=project_id,
            )
        from core.hybrid_helpers import call_with_bridge_priority
        return call_with_bridge_priority(
            is_browser_available=self._browser_available,
            browser_fn=lambda: self._browser.upload_image_from_path(
                image_path=image_path, aspect=aspect, project_id=project_id,
            ),
            httpx_fn=lambda: self._httpx.upload_image_from_path(
                image_path=image_path, aspect=aspect, project_id=project_id,
            ),
            label=f"upload_image_from_path account={self._account_email}",
        )
