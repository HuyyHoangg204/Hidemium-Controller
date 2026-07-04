import os
import sys
import time
import json
import random

from PySide6.QtCore import QThread, Signal

_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

from core.veo_client import VeoClient
from core.captcha_solver import invalidate_cached_captcha
from core.project import create_project


POLL_INTERVAL = 5      # poll mỗi 5s (giảm từ 8s → phát hiện xong sớm hơn)
POLL_INTERVAL_SLOW = 10  # sau 120s → tăng lên 10s để tránh spam
POLL_MAX_WAIT = 600


def _download_video(url, dest_path, headers=None, aspect="portrait"):
    import urllib.request

    print(f"[Download] Bắt đầu tải video: {dest_path}")
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=120) as r, open(dest_path, "wb") as f:
            while True:
                chunk = r.read(1 << 20)  # 1MB chunk (tăng từ 64KB)
                if not chunk:
                    break
                f.write(chunk)
        print(f"[Download] Đã tải xong: {dest_path}")
    except Exception as e:
        print(f"[Download] Lỗi tải video: {e}")
        raise


def _extract_video_url(poll_resp):
    if not isinstance(poll_resp, dict):
        return None

    def _search(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, str) and v.startswith("http"):
                    kl = k.lower()
                    if any(
                        x in kl for x in ("url", "uri", "download", "video", "link")
                    ):
                        return v
                    if any(
                        x in v.lower()
                        for x in (
                            "storage.googleapis",
                            ".mp4",
                            "generativelanguage",
                            "video",
                        )
                    ):
                        return v
                r = _search(v)
                if r:
                    return r
        elif isinstance(obj, list):
            for v in obj:
                r = _search(v)
                if r:
                    return r
        return None

    for op in poll_resp.get("operations", []):
        gens = op.get("mediaGenerations", [])
        for g in gens:
            url = (
                g.get("videoUrl")
                or g.get("downloadUrl")
                or g.get("uri")
                or g.get("url")
            )
            if url:
                return url
        resp_field = op.get("response") or op.get("result")
        if resp_field:
            u = _search(resp_field)
            if u:
                return u
    return _search(poll_resp)

def _extract_media_id(poll_resp):
    if not isinstance(poll_resp, dict):
        return None
    for op in poll_resp.get("operations", []):
        gens = op.get("mediaGenerations", [])
        for g in gens:
            mid = g.get("mediaId")
            if mid:
                return mid
    return None


def _extract_status(poll_resp):
    if not isinstance(poll_resp, dict):
        return "UNKNOWN"
    for op in poll_resp.get("operations", []):
        st = op.get("status", "")
        if st:
            return st
        gens = op.get("mediaGenerations", [])
        for g in gens:
            st2 = g.get("status", "")
            if st2:
                return st2
        op_obj = op.get("operation", {})
        if isinstance(op_obj, dict):
            if op_obj.get("done"):
                return "MEDIA_GENERATION_STATUS_COMPLETE"
    return "UNKNOWN"


class VeoWorker(QThread):
    status_changed = Signal(int, str, str)
    video_ready = Signal(int, int, str)
    project_created = Signal(int, str)
    retry_needed = Signal(int)  # row

    def __init__(
        self,
        row,
        prompt,
        settings,
        session_token,
        output_dir,
        start_delay=0,
        mode="text",
        image_path=None,
        end_image_path=None,
        project_id=None,
        model_key=None,
        stt_index=None,
    ):
        super().__init__()
        self.row = row
        self.prompt = prompt
        self.settings = settings
        self.session_token = session_token
        self.output_dir = output_dir
        self.start_delay = start_delay
        self.mode = mode
        self.image_path = image_path
        self.end_image_path = end_image_path
        self.project_id = project_id  # reuse existing project if provided
        self.model_key = model_key  # override model if provided
        self.stt_index = stt_index if stt_index is not None else (row + 1)
        self._stopped = False

    def stop(self):
        """Yêu cầu worker dừng ngay."""
        self._stopped = True
        self.requestInterruption()

    def _check_stop(self):
        """Return True nếu đã bị yêu cầu dừng."""
        return self._stopped or self.isInterruptionRequested()

    def run(self):
        row = self.row
        if self.start_delay > 0:
            self.status_changed.emit(row, "WAIT", "#f59e0b")
            for _ in range(int(self.start_delay)):
                if self._check_stop():
                    self.status_changed.emit(row, "STOPPED", "#ef4444")
                    return
                time.sleep(1)

        success = False
        try:
            if self._check_stop():
                self.status_changed.emit(row, "STOPPED", "#ef4444")
                return
            self.status_changed.emit(row, "AUTH", "#3b82f6")
            client = VeoClient(self.session_token)
            browser_runtime_enabled = bool(getattr(client, "_browser_runtime_enabled", lambda: False)())
            if not browser_runtime_enabled:
                print(
                    f"[VeoWorker row={row}] Shared browser runtime is required; "
                    "refusing PlaywrightCaptchaSolver fallback to preserve one-Chrome mode"
                )
                self.status_changed.emit(row, "ERROR", "#ef4444")
                self.retry_needed.emit(row)
                return

            aspect = self.settings.get("aspect", "portrait")
            aspect_ratio = (
                "VIDEO_ASPECT_RATIO_16_9"
                if aspect == "landscape"
                else "VIDEO_ASPECT_RATIO_9_16"
            )
            count = int(self.settings.get("count", 1))

            # Reuse existing project_id if provided, otherwise create new
            project_id = self.project_id
            if not project_id:
                self.status_changed.emit(row, "PROJECT", "#6c63ff")
                proxy = self.settings.get("proxy") or None
                project_id = create_project(
                    "AutoVoice",
                    tool_name="PINHOLE",
                    cookie=self.session_token,
                    access_token=client.access_token,
                    browser_headers=client.base_headers,
                    proxy=proxy,
                )
                print(f"[VeoWorker row={row}] created project_id={project_id!r}")
                if not project_id:
                    self.status_changed.emit(row, "ERROR", "#ef4444")
                    self.retry_needed.emit(row)
                    return
                self.project_created.emit(row, project_id)
            else:
                print(f"[VeoWorker row={row}] reusing project_id={project_id!r}")

            if self._check_stop():
                self.status_changed.emit(row, "STOPPED", "#ef4444")
                return
            token = "BROWSER_RUNTIME_RECAPTCHA_IN_PAGE"
            print(f"[VeoWorker row={row}] Browser runtime enabled — using shared in-page captcha/runtime")

            if self._check_stop():
                self.status_changed.emit(row, "STOPPED", "#ef4444")
                return
            self.status_changed.emit(row, "VIDEO", "#6c63ff")

            if self.mode == "image":
                start_id = None
                end_id = None
                if self.image_path and os.path.exists(self.image_path):
                    self.status_changed.emit(row, "UPLOAD", "#3b82f6")
                    start_id = client.upload_image_from_path(
                        self.image_path, project_id=project_id, aspect=aspect_ratio,
                    )
                if self.end_image_path and os.path.exists(self.end_image_path):
                    end_id = client.upload_image_from_path(
                        self.end_image_path, project_id=project_id, aspect=aspect_ratio,
                    )
                    if not end_id:
                        self.status_changed.emit(row, "ERROR", "#ef4444")
                        self.retry_needed.emit(row)
                        return

                if not start_id:
                    self.status_changed.emit(row, "ERROR", "#ef4444")
                    self.retry_needed.emit(row)
                    return
                self.status_changed.emit(row, "VIDEO", "#6c63ff")
                if end_id:
                    print(f"[Row {row}] [I2V] Using Start+End endpoint (2 images)")
                    result = client.create_video_start_end_image(
                        row=row,
                        prompt=self.prompt,
                        project_id=project_id,
                        captcha_token=token,
                        start_image_media_id=start_id,
                        end_image_media_id=end_id,
                        aspect=aspect_ratio,
                        count=count,
                    )
                else:
                    print(f"[Row {row}] [I2V] Using reference image endpoint (1 image)")
                    result = client.create_video_i2v(
                        row=row,
                        prompt=self.prompt,
                        project_id=project_id,
                        captcha_token=token,
                        start_image_media_id=start_id,
                        aspect=aspect_ratio,
                        count=count,
                    )
            else:
                result = client.create_video_t2v(
                    row=row,
                    prompt=self.prompt,
                    project_id=project_id,
                    captcha_token=token,
                    aspect=aspect_ratio,
                    count=count,
                    model_key=self.model_key,  # pass model override
                )

            if not result:
                self.status_changed.emit(row, "ERROR", "#ef4444")
                self.retry_needed.emit(row)
                return

            ops = result.get("ops", [])
            if not ops:
                self.status_changed.emit(row, "ERROR", "#ef4444")
                self.retry_needed.emit(row)
                return

            os.makedirs(self.output_dir, exist_ok=True)

            for i, op in enumerate(ops):
                op_name = op.get("operation", {}).get("name", "")
                scene_id = op.get("sceneId", result.get("scene_id", ""))
                if not op_name:
                    continue

                self.status_changed.emit(row, "POLL", "#3b82f6")
                video_url = None
                elapsed = 0
                current_status = "MEDIA_GENERATION_STATUS_PENDING"

                prev_st = None
                while elapsed < POLL_MAX_WAIT:
                    # Adaptive poll: nhanh lúc đầu, chậm dần sau 2 phút
                    _poll_step = POLL_INTERVAL if elapsed < 120 else POLL_INTERVAL_SLOW
                    for _ in range(_poll_step):
                        if self._check_stop():
                            self.status_changed.emit(row, "STOPPED", "#ef4444")
                            return
                        time.sleep(1)
                    elapsed += _poll_step

                    poll_resp = client.check_status_batch(
                        row, op_name, scene_id, current_status
                    )
                    if not poll_resp:
                        continue

                    st = _extract_status(poll_resp)
                    current_status = st

                    if st != prev_st:
                        print(
                            f"[VeoWorker row={row}] poll[{i}] elapsed={elapsed}s status={st}"
                        )
                        print(
                            f"[VeoWorker row={row}] poll[{i}] FULL={json.dumps(poll_resp)}"
                        )
                        prev_st = st
                    else:
                        print(
                            f"[VeoWorker row={row}] poll[{i}] elapsed={elapsed}s status={st}"
                        )

                    if "ACTIVE" in st or "PENDING" in st or st == "UNKNOWN":
                        continue

                    video_url = _extract_video_url(poll_resp)
                    media_id = _extract_media_id(poll_resp)

                    if "COMPLETE" in st or "SUCCESS" in st or "DONE" in st:
                        if not video_url:
                            print(
                                f"[VeoWorker row={row}] poll[{i}] COMPLETE but no URL, scanning full resp..."
                            )
                        break
                    elif "FAIL" in st or "ERROR" in st or "CANCEL" in st:
                        print(f"[VeoWorker row={row}] poll[{i}] terminal status={st}")
                        break
                    else:
                        if video_url:
                            print(
                                f"[VeoWorker row={row}] poll[{i}] unknown status={st} but URL found, using it"
                            )
                            break

                if not video_url:
                    self.video_ready.emit(row, i, f"[timeout/fail] {op_name[:40]}")
                    continue

                # ----- NATIVE UPSCALE 1080p -----
                upscaled_url = None
                if media_id:
                    print(f"[VeoWorker row={row}] Yêu cầu Upscale 1080p cho mediaId={media_id[:20]}...")
                    try:
                        import uuid
                        upscale_res = client.upscale_video(
                            row=row,
                            input_media_generation_id=media_id,
                            project_id=project_id,
                            captcha_token=token
                        )
                        if upscale_res and upscale_res.get("op_name"):
                            up_op_name = upscale_res["op_name"]
                            up_scene_id = upscale_res["scene_id"]
                            self.status_changed.emit(row, "UPSCALE", "#3b82f6")

                            up_elapsed = 0
                            up_status = "PENDING"
                            while up_elapsed < POLL_MAX_WAIT:
                                # Adaptive poll cho upscale
                                _up_step = POLL_INTERVAL if up_elapsed < 120 else POLL_INTERVAL_SLOW
                                for _ in range(_up_step):
                                    if self._check_stop():
                                        self.status_changed.emit(row, "STOPPED", "#ef4444")
                                        return
                                    time.sleep(1)
                                up_elapsed += _up_step
                                up_resp = client.check_status_batch(row, up_op_name, up_scene_id, up_status)
                                if not up_resp:
                                    continue

                                ust = _extract_status(up_resp)
                                up_status = ust
                                if "ACTIVE" in ust or "PENDING" in ust or ust == "UNKNOWN":
                                    continue

                                u_url = _extract_video_url(up_resp)
                                if "COMPLETE" in ust or "SUCCESS" in ust or "DONE" in ust:
                                    upscaled_url = u_url
                                    break
                                elif "FAIL" in ust or "ERROR" in ust or "CANCEL" in ust:
                                    break
                                else:
                                    if u_url:
                                        upscaled_url = u_url
                                        break

                            if upscaled_url:
                                print(f"[VeoWorker row={row}] Upscale thành công!")
                        else:
                            print(f"[VeoWorker row={row}] upscale_video trả về rỗng, dùng video gốc (720p).")
                    except Exception as ue:
                        print(f"[VeoWorker row={row}] Lỗi upscale: {ue}. Dùng video gốc.")
                
                final_url = upscaled_url if upscaled_url else video_url
                # --------------------------------

                safe_prompt = "".join(
                    c if c.isalnum() or c in " -_" else "" for c in self.prompt
                )[:40].strip()

                # Folder: outputs/{project_id_short}/{YYYY-MM-DD}/
                from datetime import datetime
                date_str = datetime.now().strftime("%Y-%m-%d")
                proj_folder = (project_id or "no_project")[:12]
                out_dir = os.path.join(self.output_dir, proj_folder, date_str)
                os.makedirs(out_dir, exist_ok=True)

                # Filename: {STT}_{PartN_}{prompt}.mp4  (spaces → underscores)
                stt_prefix = f"{self.stt_index:02d}"
                if count > 1:
                    fname = f"{stt_prefix}_{i+1}_{safe_prompt}.mp4"
                else:
                    fname = f"{stt_prefix}_{safe_prompt}.mp4"
                fname = fname.replace(" ", "_")

                base_fname, ext = os.path.splitext(fname)
                dest = os.path.join(out_dir, fname)
                counter = 1
                while os.path.exists(dest):
                    dest = os.path.join(out_dir, f"{base_fname}({counter}){ext}")
                    counter += 1

                print(f"[VeoWorker row={row}] Output path: {dest}")

                self.status_changed.emit(row, "DOWNLOAD", "#22c55e")
                try:
                    dl_headers = {
                        "User-Agent": client.base_headers.get("User-Agent", "")
                    }
                    _download_video(
                        final_url, dest,
                        headers=dl_headers,
                        aspect=aspect,
                    )
                    print(f"[VeoWorker row={row}] ✅ Saved: {dest}")
                    self.video_ready.emit(row, i, dest)
                except Exception as de:
                    print(f"[VeoWorker row={row}] Download error: {de}")
                    self.video_ready.emit(row, i, video_url)

            success = True
            
            # --- GIỮ LẠI ảnh gốc để hỗ trợ retry sau này ---
            # (Trước đây xóa ảnh ở đây gây lỗi khi retry I2V)
            # if self.mode == "image":
            #     for img_p in [self.image_path, self.end_image_path]:
            #         if img_p and os.path.exists(img_p):
            #             os.remove(img_p)
            # -----------------------------------------------

            self.status_changed.emit(row, "DONE", "#22c55e")

        except Exception as e:
            print(f"[VeoWorker row={row}] Lỗi xử lý video: {e}")
            self.status_changed.emit(row, "ERROR", "#ef4444")
            if not success:
                self.retry_needed.emit(row)
