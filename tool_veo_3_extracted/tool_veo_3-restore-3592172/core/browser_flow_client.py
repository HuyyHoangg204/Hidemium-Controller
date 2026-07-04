"""
browser_flow_client.py — Client gọi Google Labs API qua Chrome extension
bridge (`browser_task_server`). Bypass UNUSUAL_ACTIVITY vì request chạy trong
tab labs.google của account đó (TLS thật, cookie thật, reCAPTCHA score cao).

Interface tương đồng với `core.imagen_client.ImagenClient` để dễ migrate
trong `web/veo_service.py`.

Usage:
    client = BrowserFlowClient(account_email="abc@gmail.com")
    if not client.is_connected():
        # fallback httpx
        ...
    res = client.generate_image(prompt="...", project_id="...", aspect="...")
"""

from __future__ import annotations

import json
import logging
import random
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

GENERATE_URL_TPL = (
    "https://aisandbox-pa.googleapis.com/v1/projects/{project_id}"
    "/flowMedia:batchGenerateImages"
)
UPLOAD_URL = "https://aisandbox-pa.googleapis.com/v1/flow/uploadImage"
T2V_URL = "https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoText"
I2V_URL = "https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoReferenceImages"
START_END_URL = "https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoStartAndEndImage"

# Tab UI URL theo project — extension navigate tab tới đây trước khi fetch
# → grecaptcha có context đầy đủ (đang trong project) → score cao hơn home /flow
PROJECT_URL_TPL = "https://labs.google/fx/vi/tools/flow/project/{project_id}"


class BrowserFlowClient:
    """Client gọi qua extension. KHÔNG quản lý cookie/proxy/access_token —
    extension tự xử lý vì chạy trong context tab labs.google."""

    DEFAULT_TIMEOUT = 90  # giây block đợi extension trả result

    def __init__(self, account_email: str, timeout: int = DEFAULT_TIMEOUT):
        self.account_email = account_email
        self.timeout = timeout
        # session_id giả lập giống ImagenClient — extension sẽ thay khi build payload
        self._session_id = f";{int(time.time() * 1000)}"
        self._last_error_detail: Optional[str] = None

    # ── Connection state ─────────────────────────────────────────────────────

    def is_connected(self) -> bool:
        """Chrome của account này đang register với bridge không?"""
        try:
            from core.browser_task_server import is_account_connected
        except Exception:
            return False
        return is_account_connected(self.account_email)

    def reload_tab(self, timeout_s: int = 30) -> bool:
        """F5 tab labs.google của account này.

        Gọi sau khi gặp lỗi F5-recoverable (UNUSUAL_ACTIVITY / 429 too much /
        RESOURCE_EXHAUSTED / 5xx) — behavior giống user F5 manual để
        grecaptcha có context mới + page state reset.

        Trả True nếu reload thành công (tab onUpdated complete + sleep 4-8s),
        False nếu bridge offline / timeout / lỗi extension.
        """
        if not self.is_connected():
            logger.warning(
                f"[BrowserFlow] reload_tab: account {self.account_email} chưa connect"
            )
            return False
        try:
            res = self._dispatch("reload_tab", {}, timeout=timeout_s)
        except Exception as e:
            logger.warning(f"[BrowserFlow] reload_tab dispatch exception: {e}")
            return False
        if res.get("ok") and res.get("status") == 200:
            logger.info(f"[BrowserFlow] reload_tab OK → {self.account_email}")
            return True
        err = res.get("error") or f"status={res.get('status')}"
        logger.warning(f"[BrowserFlow] reload_tab FAIL ({self.account_email}): {err}")
        return False

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_aspect(aspect: str) -> str:
        a = (aspect or "").upper().strip()
        if "PORTRAIT" in a or "9:16" in a:
            return "IMAGE_ASPECT_RATIO_PORTRAIT"
        if "SQUARE" in a or "1:1" in a:
            return "IMAGE_ASPECT_RATIO_SQUARE"
        return "IMAGE_ASPECT_RATIO_LANDSCAPE"

    def _dispatch(self, action: str, payload: dict, timeout: Optional[int] = None) -> dict:
        try:
            from core.browser_task_server import request_browser_task
        except Exception as e:
            return {"ok": False, "error": f"import_fail: {e}"}
        return request_browser_task(
            account=self.account_email,
            action=action,
            payload=payload,
            timeout=timeout or self.timeout,
        )

    def _build_payload_image(
        self,
        prompt: str,
        project_id: str,
        aspect_norm: str,
        model: str = "GEM_PIX_2",
        seed: Optional[int] = None,
        count: int = 1,
        image_media_ids: Optional[list[str]] = None,
    ) -> dict:
        if seed is None:
            seed = random.randint(100000, 999999)
        # Captcha sẽ được thay bằng __CAPTCHA_TOKEN__ → extension chèn token thật
        ctx = {
            "projectId": project_id,
            "tool": "PINHOLE",
            "sessionId": self._session_id,
            "recaptchaContext": {
                "token": "__CAPTCHA_TOKEN__",
                "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
            },
        }
        image_inputs: list[dict] = []
        if image_media_ids:
            for mid in image_media_ids:
                if mid:
                    image_inputs.append({
                        "imageInputType": "IMAGE_INPUT_TYPE_REFERENCE",
                        "name": mid,
                    })
        requests_list = []
        for _ in range(max(1, count)):
            s = random.randint(100000, 999999) if count > 1 else seed
            requests_list.append({
                "clientContext": ctx,
                "seed": s,
                "imageModelName": model,
                "imageAspectRatio": aspect_norm,
                "structuredPrompt": {"parts": [{"text": prompt}]},
                "imageInputs": image_inputs,
            })
        return {
            "clientContext": ctx,
            "mediaGenerationContext": {"batchId": str(uuid.uuid4())},
            "useNewMedia": True,
            "requests": requests_list,
        }

    # ── Public API ──────────────────────────────────────────────────────────

    def generate_image(
        self,
        prompt: str,
        project_id: str,
        aspect: str = "IMAGE_ASPECT_RATIO_LANDSCAPE",
        model: str = "GEM_PIX_2",
        seed: Optional[int] = None,
        count: int = 1,
        image_media_ids: Optional[list[str]] = None,
        row: int = 0,
    ) -> Optional[dict]:
        """Trả response JSON (dict) hoặc None nếu fail. Set self._last_error_detail."""
        aspect_norm = self._normalize_aspect(aspect)
        payload_body = self._build_payload_image(
            prompt=prompt, project_id=project_id, aspect_norm=aspect_norm,
            model=model, seed=seed, count=count, image_media_ids=image_media_ids,
        )
        url = GENERATE_URL_TPL.format(project_id=project_id)
        bridge_payload = {
            "url": url,
            "method": "POST",
            "captcha_action": "IMAGE_GENERATION",
            "body_template": json.dumps(payload_body),
            # Global browser rule: reuse the already-registered Labs page.
            # Do not ask the extension to open/navigate a new project tab per task.
            "reuse_current_page": True,
            "no_new_tab": True,
        }

        logger.info(f"[BrowserFlow] Dispatch generate_image → {self.account_email} prompt={prompt[:60]!r}")
        res = self._dispatch("auto_fetch", bridge_payload, timeout=180)
        if not res.get("ok"):
            self._last_error_detail = res.get("error") or f"HTTP {res.get('status')}: {(res.get('body') or '')[:300]}"
            logger.warning(f"[BrowserFlow] generate_image FAIL: {self._last_error_detail}")
            return None
        body = res.get("body") or "{}"
        try:
            return json.loads(body)
        except Exception as e:
            self._last_error_detail = f"json_parse_error: {e}; body={body[:200]}"
            return None

    def upload_user_image(
        self,
        image_base64: str,
        project_id: str,
        mime: str = "image/jpeg",
    ) -> Optional[str]:
        """Upload ảnh ref qua extension. Trả media_id hoặc None."""
        body = json.dumps({
            "clientContext": {
                "sessionId": self._session_id,
                "tool": "PINHOLE",
                "projectId": project_id,
            },
            "imageBytes": image_base64,
        })
        bridge_payload = {
            "url": UPLOAD_URL,
            "method": "POST",
            "body_template": body,
            "reuse_current_page": True,
            "no_new_tab": True,
            # KHÔNG có captcha_action — upload không cần captcha
        }
        logger.info(f"[BrowserFlow] Dispatch upload → {self.account_email} (size_b64={len(image_base64)})")
        res = self._dispatch("auto_fetch", bridge_payload)
        if not res.get("ok"):
            self._last_error_detail = res.get("error") or f"HTTP {res.get('status')}: {(res.get('body') or '')[:300]}"
            logger.warning(f"[BrowserFlow] upload_user_image FAIL: {self._last_error_detail}")
            return None
        try:
            jr = json.loads(res.get("body") or "{}")
        except Exception as e:
            self._last_error_detail = f"json_parse_error: {e}"
            return None
        media_id = (jr.get("media", {}) or {}).get("name") or jr.get("imageId") or jr.get("id")
        if not media_id:
            self._last_error_detail = f"no_media_id; keys={list(jr.keys())}"
            return None
        return media_id

    def upload_image_from_path(self, image_path: str, aspect: str = "IMAGE_ASPECT_RATIO_LANDSCAPE",
                                project_id: Optional[str] = None) -> Optional[str]:
        """Đọc file → base64 → upload qua extension. Tương đương VeoClient."""
        import base64, mimetypes, os
        try:
            with open(image_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode("ascii")
        except Exception as e:
            self._last_error_detail = f"read_image_fail: {e}"
            return None
        mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
        return self.upload_user_image(image_base64=img_b64, project_id=project_id or "", mime=mime)

    # ── Video methods (T2V / I2V / Start-End) ───────────────────────────────

    @staticmethod
    def _normalize_video_aspect(aspect: str) -> str:
        a = (aspect or "").upper().strip()
        if any(k in a for k in ("9_16", "9:16", "PORTRAIT")):
            return "VIDEO_ASPECT_RATIO_PORTRAIT"
        return "VIDEO_ASPECT_RATIO_LANDSCAPE"

    @staticmethod
    def _gen_seeds(count: int, seed: Optional[int], scene_ids: Optional[list]):
        seeds, ids = [], []
        for i in range(max(1, count)):
            seeds.append(int(seed) if seed is not None else random.randint(10000, 99999))
            ids.append(scene_ids[i] if scene_ids and i < len(scene_ids) else str(uuid.uuid4()))
        return seeds, ids

    def _video_dispatch_and_parse(self, url: str, payload: dict, scene_ids: list, tag: str) -> Optional[dict]:
        # Global browser rule: use the already-registered Labs page; do not open/navigate per-task tabs.
        try:
            project_id = payload.get("clientContext", {}).get("projectId")
        except Exception:
            project_id = None
        bridge_payload = {
            "url": url,
            "method": "POST",
            "captcha_action": "VIDEO_GENERATION",
            "body_template": json.dumps(payload),
            "reuse_current_page": True,
            "no_new_tab": True,
        }
        logger.info(f"[BrowserFlow] Dispatch {tag} → {self.account_email}")
        res = self._dispatch("auto_fetch", bridge_payload, timeout=180)
        if not res.get("ok"):
            self._last_error_detail = res.get("error") or f"HTTP {res.get('status')}: {(res.get('body') or '')[:300]}"
            logger.warning(f"[BrowserFlow] {tag} FAIL: {self._last_error_detail}")
            return None
        try:
            jr = json.loads(res.get("body") or "{}")
        except Exception as e:
            self._last_error_detail = f"json_parse_error: {e}"
            return None
        ops = jr.get("operations", [])
        if not ops:
            self._last_error_detail = f"no_operations; keys={list(jr.keys())}"
            return None
        try:
            op_name = ops[0]["operation"]["name"]
        except Exception:
            self._last_error_detail = f"no_op_name; ops[0]={ops[0]}"
            return None
        return {"op_name": op_name, "scene_id": scene_ids[0], "ops": ops}

    def _build_video_ctx(self, project_id: str) -> dict:
        return {
            "projectId": project_id,
            "tool": "PINHOLE",
            "userPaygateTier": "PAYGATE_TIER_TWO",
            "sessionId": self._session_id,
            "recaptchaContext": {
                "token": "__CAPTCHA_TOKEN__",
                "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
            },
        }

    def create_video_t2v(self, row, prompt, project_id, captcha_token,
                          aspect="VIDEO_ASPECT_RATIO_PORTRAIT", model_key=None,
                          seed=None, scene_ids=None, count=1, reference_image_id=None):
        aspect_norm = self._normalize_video_aspect(aspect)
        if not model_key:
            model_key = ("veo_3_1_t2v_fast_ultra"
                         if aspect_norm == "VIDEO_ASPECT_RATIO_PORTRAIT"
                         else "veo_3_1_t2v_fast_ultra")
        seeds, ids = self._gen_seeds(count, seed, scene_ids)
        ctx = self._build_video_ctx(project_id)
        requests_list = []
        for i in range(max(1, count)):
            req = {
                "aspectRatio": aspect_norm,
                "seed": seeds[i],
                "textInput": {"structuredPrompt": {"parts": [{"text": prompt}]}},
                "videoModelKey": model_key,
                "metadata": {},
            }
            if reference_image_id:
                req["referenceImages"] = [{
                    "mediaId": reference_image_id,
                    "imageUsageType": "IMAGE_USAGE_TYPE_ASSET",
                }]
            requests_list.append(req)
        payload = {
            "mediaGenerationContext": {"batchId": str(uuid.uuid4())},
            "clientContext": ctx,
            "requests": requests_list,
            "useV2ModelConfig": True,
        }
        return self._video_dispatch_and_parse(T2V_URL, payload, ids, tag="T2V")

    def create_video_i2v(self, row, prompt, project_id, captcha_token,
                          start_image_media_id, end_image_media_id=None,
                          aspect="VIDEO_ASPECT_RATIO_9_16", seed=None,
                          scene_ids=None, count=1, model_key=None):
        aspect_norm = self._normalize_video_aspect(aspect)
        if not model_key:
            model_key = ("veo_3_1_r2v_fast_portrait_ultra"
                         if aspect_norm == "VIDEO_ASPECT_RATIO_PORTRAIT"
                         else "veo_3_1_r2v_fast_landscape_ultra")
        seeds, ids = self._gen_seeds(count, seed, scene_ids)
        ctx = self._build_video_ctx(project_id)
        requests_list = []
        for i in range(max(1, count)):
            requests_list.append({
                "aspectRatio": aspect_norm,
                "seed": seeds[i],
                "textInput": {"structuredPrompt": {"parts": [{"text": prompt}]}},
                "videoModelKey": model_key,
                "referenceImages": [{
                    "mediaId": start_image_media_id,
                    "imageUsageType": "IMAGE_USAGE_TYPE_ASSET",
                }],
                "metadata": {},
            })
        payload = {
            "mediaGenerationContext": {"batchId": str(uuid.uuid4())},
            "clientContext": ctx,
            "requests": requests_list,
            "useV2ModelConfig": True,
        }
        return self._video_dispatch_and_parse(I2V_URL, payload, ids, tag="I2V")

    def create_video_start_end_image(self, row, prompt, project_id, captcha_token,
                                       start_image_media_id, end_image_media_id,
                                       aspect="VIDEO_ASPECT_RATIO_PORTRAIT",
                                       seed=None, count=1, model_key=None):
        aspect_norm = self._normalize_video_aspect(aspect)
        if not model_key:
            model_key = "veo_3_1_i2v_s_fast_ultra"
        seeds, _ = self._gen_seeds(count, seed, None)
        batch_id = str(uuid.uuid4())
        ctx = self._build_video_ctx(project_id)
        requests_list = []
        for i in range(max(1, count)):
            requests_list.append({
                "aspectRatio": aspect_norm,
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
            })
        payload = {
            "mediaGenerationContext": {
                "batchId": batch_id,
                "audioFailurePreference": "BLOCK_SILENCED_VIDEOS",
            },
            "clientContext": ctx,
            "requests": requests_list,
            "useV2ModelConfig": True,
        }
        return self._video_dispatch_and_parse(START_END_URL, payload, [batch_id], tag="I2V-SE")
