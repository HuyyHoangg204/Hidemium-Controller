import os
import sys
import time

from PySide6.QtCore import QThread, Signal

_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

from core.veo_client import VeoClient
from core.project import create_project


class ImagenWorker(QThread):
    status_changed = Signal(int, str, str)
    image_ready = Signal(int, int, str)
    project_created = Signal(int, str)
    retry_needed = Signal(int)

    def __init__(
        self,
        row,
        prompt,
        settings,
        session_token,
        output_dir,
        start_delay=0,
        project_id=None,
    ):
        super().__init__()
        self.row = row
        self.prompt = prompt
        self.settings = settings
        self.session_token = session_token
        self.output_dir = output_dir
        self.start_delay = start_delay
        self.project_id = project_id
        self.success = False
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
            # Sleep chunked để dừng nhanh
            for _ in range(int(self.start_delay)):
                if self._check_stop():
                    self.status_changed.emit(row, "STOPPED", "#ef4444")
                    return
                time.sleep(1)

        try:
            # ---- AUTH + PROJECT: dùng VeoClient (giống hệt VeoWorker) ----
            if self._check_stop():
                self.status_changed.emit(row, "STOPPED", "#ef4444")
                return
            self.status_changed.emit(row, "AUTH", "#3b82f6")
            veo = VeoClient(self.session_token)
            from core.banana_runtime.scheduler import BananaJob, BananaScheduler

            project_id = self.project_id
            if not project_id:
                self.status_changed.emit(row, "PROJECT", "#6c63ff")
                project_id = create_project(
                    "AutoVoice",
                    tool_name="PINHOLE",
                    cookie=self.session_token,
                    access_token=veo.access_token,
                    browser_headers=veo.base_headers,
                )
                print(f"[ImagenWorker row={row}] created project_id={project_id!r}")
                if not project_id:
                    self.status_changed.emit(row, "ERROR", "#ef4444")
                    self.retry_needed.emit(row)
                    return
                self.project_created.emit(row, project_id)
            else:
                print(f"[ImagenWorker row={row}] reusing project_id={project_id!r}")

            # ---- GENERATE VIA GLOBAL BANANA RUNTIME ----
            # One-Chrome rule: do not use PlaywrightCaptchaSolver/browser_post_json here.
            self.status_changed.emit(row, "IMAGE_GEN", "#6c63ff")
            aspect_raw = self.settings.get("aspect", "IMAGE_ASPECT_RATIO_PORTRAIT")
            img_count = int(self.settings.get("img_count", 4))
            model = self.settings.get("model", "NARWHAL")
            resolution = self.settings.get("resolution", "4K")
            image_refs = self.settings.get("image_refs", []) or []
            thread_count = int(self.settings.get("thread_count", self.settings.get("banana_thread_count", 1)) or 1)
            max_attempts = int(self.settings.get("max_attempts", self.settings.get("banana_max_attempts", 5)) or 5)
            tokens = []
            if getattr(veo, "access_token", None):
                tokens.append(veo.access_token)
            if not tokens:
                self.status_changed.emit(row, "ERROR", "#ef4444")
                self.retry_needed.emit(row)
                return

            jobs = []
            output_paths = []
            for idx in range(img_count):
                out_path = os.path.join(self.output_dir, f"imagen_{row}_{idx+1}_{resolution}_{int(time.time())}.png")
                output_paths.append(out_path)
                jobs.append(
                    BananaJob(
                        prompt=self.prompt,
                        model=model,
                        aspect_ratio={
                            "IMAGE_ASPECT_RATIO_LANDSCAPE": "16:9",
                            "IMAGE_ASPECT_RATIO_SQUARE": "1:1",
                        }.get(aspect_raw, "9:16"),
                        reference_path=image_refs[0] if image_refs else None,
                        output_path=out_path,
                        job_id=f"local-image-{row}-{idx+1}",
                    )
                )

            scheduler = BananaScheduler(tokens=tokens, thread_count=thread_count, max_attempts=max_attempts)
            results = scheduler.submit(jobs)
            result_paths = [r.saved_path for r in results if r.status == "completed" and r.saved_path]
            if not result_paths:
                self.status_changed.emit(row, "ERROR", "#ef4444")
                self.retry_needed.emit(row)
                return

            for idx, out_path in enumerate(result_paths):
                self.image_ready.emit(row, idx, out_path)
            self.success = True
            self.status_changed.emit(row, "DONE", "#22c55e")
            print(f"[ImagenWorker row={row}] Completed via global Banana runtime: {len(result_paths)} image(s)")
            return

            if not result or not isinstance(result, list) or len(result) == 0:
                self.status_changed.emit(row, "ERROR", "#ef4444")
                self.retry_needed.emit(row)
                return

            print(f"[ImagenWorker row={row}] Generated {len(result)} image(s)")

            # ---- Upscale + SAVE từng ảnh ----
            resolution = self.settings.get("resolution", "4K")
            saved_count = 0

            for idx, img_info in enumerate(result):
                if self._check_stop():
                    self.status_changed.emit(row, "STOPPED", "#ef4444")
                    return
                media_id = img_info.get("media_id")
                if not media_id:
                    print(f"[ImagenWorker row={row}] Image {idx+1}: no media_id, skip")
                    continue

                self.status_changed.emit(
                    row, f"Upscale {idx+1}/{len(result)}", "#a855f7"
                )
                print(
                    f"[ImagenWorker row={row}] Upsampling image {idx+1}/{len(result)}: {media_id[:20]}..."
                )

                # Captcha mới cho mỗi upsample
                upsample_captcha = None
                for attempt in range(1, 4):
                    try:
                        upsample_captcha = solver.solve_imagen(row=row)
                        if upsample_captcha:
                            break
                    except Exception as ce:
                        print(
                            f"[ImagenWorker row={row}] Upscale captcha attempt {attempt} error: {ce}"
                        )
                    if attempt < 3:
                        time.sleep(2)

                image_bytes = None
                if upsample_captcha:
                    image_bytes = imagen.upsample_image(
                        row=row,
                        media_id=media_id,
                        resolution=resolution,
                        project_id=project_id,
                        captcha_token=upsample_captcha,
                    )

                # Fallback: download ảnh gốc nếu Upscale fail
                if not image_bytes:
                    print(
                        f"[ImagenWorker row={row}] Image {idx+1}: Upscale failed, downloading original..."
                    )
                    image_bytes = imagen.download_image(row, media_id)
                    if not image_bytes:
                        fife_url = img_info.get("fife_url")
                        if fife_url:
                            image_bytes = imagen.download_from_fife(row, fife_url)

                if image_bytes:
                    self.status_changed.emit(
                        row, f"DOWNLOAD {idx+1}/{len(result)}", "#22d3ee"
                    )
                    out_path = self._save_image(image_bytes, row, resolution, idx)
                    print(
                        f"[ImagenWorker row={row}] Image {idx+1} saved: {out_path} ({len(image_bytes)} bytes)"
                    )
                    self.image_ready.emit(row, idx, out_path)
                    saved_count += 1
                else:
                    print(
                        f"[ImagenWorker row={row}] Image {idx+1}: all download methods failed"
                    )

            if saved_count > 0:
                self.success = True
                self.status_changed.emit(row, "DONE", "#22c55e")
                print(
                    f"[ImagenWorker row={row}] Completed: {saved_count}/{len(result)} images saved"
                )
            else:
                self.status_changed.emit(row, "ERROR", "#ef4444")
                self.retry_needed.emit(row)

        except Exception as e:
            import traceback

            traceback.print_exc()
            print(f"[ImagenWorker row={row}] Error: {e}")
            self.status_changed.emit(row, "ERROR", "#ef4444")
            self.retry_needed.emit(row)

    def _save_image(self, image_bytes, row, resolution, idx=0):
        os.makedirs(self.output_dir, exist_ok=True)
        timestamp = int(time.time())
        filename = f"imagen_{row}_{idx+1}_{resolution}_{timestamp}.png"
        out_path = os.path.join(self.output_dir, filename)
        with open(out_path, "wb") as f:
            f.write(image_bytes)
        return out_path
