"""
Test nano API qua BananaService logic — Python script độc lập.

Chạy:
    python services/test_nano.py --nano-key NANO_API_KEY --flask-url http://localhost:8000 [--mode all]

Modes:
    image        — Test 1: tạo ảnh từ prompt (no ref)
    video-t2v    — Test 2: tạo video từ prompt (no image)
    video-i2v    — Test 3a: tạo video I2V (chỉ start frame)
    video-frames — Test 3b: tạo video từ start + end frame
    all          — Chạy hết 4 test (default)

Token/Cookie:
    Pull tự động từ Flask /api/banana/token-current?key=admin1103
    (extension đã push qua /api/banana/token-ingest).

Output:
    File ảnh/video lưu trong ./test_nano_output/
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

import requests

# ── Constants ─────────────────────────────────────────────────────────────────
NANO_BASE = "https://flow-api.nanoai.pics"
TEST_IMAGE = "output/imagen_0c79a0b8_1_orig_1775707174.png"   # Ảnh test do user chỉ định
TEST_END_IMAGE = "output/imagen_1057d18a_1_orig_1775707640.png"  # Ảnh end frame (ảnh khác cùng folder)
TEST_PROMPT_IMAGE = "a beautiful 3D pixar style character portrait, soft lighting"
TEST_PROMPT_VIDEO = "the character looks around, soft camera pan, cinematic"
POLL_INTERVAL = 3
POLL_TIMEOUT_IMAGE = 600
POLL_TIMEOUT_VIDEO = 1200
OUTPUT_DIR = Path("test_nano_output")


# ── Helpers ───────────────────────────────────────────────────────────────────
def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def file_to_base64(path: str) -> str:
    """Đọc file ảnh → base64 string (KHÔNG kèm data: prefix)."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Ảnh test không tồn tại: {p.resolve()}")
    return base64.b64encode(p.read_bytes()).decode("ascii")


def get_veo_credentials(flask_url: str, key: str = "admin1103") -> dict:
    """Pull VEO token+cookie mới nhất từ Flask."""
    url = f"{flask_url.rstrip('/')}/api/banana/token-current?key={key}"
    r = requests.get(url, timeout=10)
    if r.status_code != 200:
        raise RuntimeError(
            f"Flask token endpoint trả {r.status_code}: {r.text[:200]}\n"
            f"→ Bật extension và đợi nó POST tới /api/banana/token-ingest trước khi test."
        )
    data = r.json()
    if not data.get("token") or not data.get("cookie"):
        raise RuntimeError("Flask trả OK nhưng thiếu token/cookie.")
    log(f"VEO credentials OK (token len={len(data['token'])}, cookie len={len(data['cookie'])})")
    return {"token": data["token"], "cookie": data["cookie"]}


def nano_post(path: str, body: dict, nano_key: str) -> dict:
    url = f"{NANO_BASE}{path}"
    r = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {nano_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        data=json.dumps(body),
        timeout=60,
    )
    text = r.text
    try:
        data = r.json()
    except Exception:
        data = {"_raw": text}
    if not r.ok:
        raise RuntimeError(f"nano {path} HTTP {r.status_code}: {text[:400]}")
    return data


def nano_get_task(task_id: str, nano_key: str) -> dict:
    url = f"{NANO_BASE}/api/v2/task?taskId={task_id}"
    r = requests.get(
        url,
        headers={
            "Authorization": f"Bearer {nano_key}",
            "Accept": "application/json",
        },
        timeout=20,
    )
    try:
        return r.json()
    except Exception:
        return {"_raw": r.text}


def poll_task(task_id: str, nano_key: str, is_video: bool = False) -> dict:
    timeout_sec = POLL_TIMEOUT_VIDEO if is_video else POLL_TIMEOUT_IMAGE
    started = time.time()
    last_status = None
    while True:
        if time.time() - started > timeout_sec:
            raise RuntimeError(f"Poll timeout sau {timeout_sec}s — taskId={task_id}")
        try:
            data = nano_get_task(task_id, nano_key)
        except Exception as e:
            log(f"  Poll error (sẽ retry): {e}")
            time.sleep(POLL_INTERVAL)
            continue
        status = (data.get("status") or data.get("data", {}).get("status") or "").upper()
        if status != last_status:
            log(f"  status={status or '<empty>'} (elapsed {int(time.time()-started)}s)")
            last_status = status
        if status == "COMPLETED":
            return data
        if status == "FAILED":
            raise RuntimeError(f"Task FAILED: {json.dumps(data)[:400]}")
        time.sleep(POLL_INTERVAL)


def extract_url(result: dict, is_video: bool) -> str | None:
    """Trích imageUrl/videoUrl từ response. Cover các format khác nhau."""
    if is_video:
        return (
            result.get("videoUrl")
            or result.get("data", {}).get("videoUrl")
            or result.get("video_url")
            or result.get("data", {}).get("video_url")
        )
    return (
        result.get("imageUrl")
        or (result.get("imageUrls") or [None])[0]
        or result.get("data", {}).get("imageUrl")
        or result.get("image_url")
        or result.get("data", {}).get("image_url")
    )


def download_to(out_path: Path, url: str) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    out_path.write_bytes(r.content)
    log(f"  💾 Saved → {out_path}  ({len(r.content)} bytes)")
    return out_path


# ── Test cases ────────────────────────────────────────────────────────────────
def test_create_image(nano_key: str, creds: dict, prompt: str = TEST_PROMPT_IMAGE) -> dict:
    """Test 1: tạo ảnh từ prompt (không có ref image)."""
    log("─" * 60)
    log("TEST 1: Create image from prompt (no ref)")
    body = {
        "accessToken": creds["token"],
        "promptText": prompt,
        "imageUrls": [],
        "aspectRatio": "IMAGE_ASPECT_RATIO_LANDSCAPE",
        "imageModel": "GEM_PIX_2",
    }
    r = nano_post("/api/v2/images/create", body, nano_key)
    task_id = r.get("taskId") or r.get("id") or r.get("data", {}).get("taskId")
    if not task_id:
        raise RuntimeError(f"Không có taskId: {json.dumps(r)[:400]}")
    log(f"  ✓ Created taskId={task_id}")
    final = poll_task(task_id, nano_key, is_video=False)
    url = extract_url(final, is_video=False)
    if not url:
        raise RuntimeError(f"Không có imageUrl trong kết quả: {json.dumps(final)[:400]}")
    out_path = OUTPUT_DIR / f"test1_image_{task_id}.jpg"
    download_to(out_path, url)
    return {"task_id": task_id, "url": url, "file": str(out_path)}


def test_create_video_t2v(nano_key: str, creds: dict, prompt: str = TEST_PROMPT_VIDEO) -> dict:
    """Test 2: tạo video từ prompt (T2V — không có ảnh đầu)."""
    log("─" * 60)
    log("TEST 2: Create video from prompt (T2V, no image)")
    body = {
        "accessToken": creds["token"],
        "promptText": prompt,
        "imageUrls": [],
        "aspectRatio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "videoModel": "VEO_3_FAST",
    }
    r = nano_post("/api/v2/videos/create", body, nano_key)
    task_id = r.get("taskId") or r.get("id") or r.get("data", {}).get("taskId")
    if not task_id:
        raise RuntimeError(f"Không có taskId: {json.dumps(r)[:400]}")
    log(f"  ✓ Created taskId={task_id}")
    final = poll_task(task_id, nano_key, is_video=True)
    url = extract_url(final, is_video=True)
    if not url:
        raise RuntimeError(f"Không có videoUrl trong kết quả: {json.dumps(final)[:400]}")
    out_path = OUTPUT_DIR / f"test2_video_t2v_{task_id}.mp4"
    download_to(out_path, url)
    return {"task_id": task_id, "url": url, "file": str(out_path)}


def test_create_video_i2v(nano_key: str, creds: dict, image_b64: str,
                          prompt: str = TEST_PROMPT_VIDEO) -> dict:
    """Test 3a: tạo video từ 1 ảnh start (I2V)."""
    log("─" * 60)
    log("TEST 3a: Create video from 1 start image (I2V)")
    body = {
        "accessToken": creds["token"],
        "promptText": prompt,
        "imageUrls": [image_b64],
        "aspectRatio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "videoModel": "VEO_3_FAST",
        "type": "frame",
    }
    r = nano_post("/api/v2/videos/create", body, nano_key)
    task_id = r.get("taskId") or r.get("id") or r.get("data", {}).get("taskId")
    if not task_id:
        raise RuntimeError(f"Không có taskId: {json.dumps(r)[:400]}")
    log(f"  ✓ Created taskId={task_id}")
    final = poll_task(task_id, nano_key, is_video=True)
    url = extract_url(final, is_video=True)
    if not url:
        raise RuntimeError(f"Không có videoUrl trong kết quả: {json.dumps(final)[:400]}")
    out_path = OUTPUT_DIR / f"test3a_video_i2v_{task_id}.mp4"
    download_to(out_path, url)
    return {"task_id": task_id, "url": url, "file": str(out_path)}


def test_create_video_frames(nano_key: str, creds: dict, start_b64: str, end_b64: str,
                             prompt: str = TEST_PROMPT_VIDEO) -> dict:
    """Test 3b: tạo video từ start + end frame."""
    log("─" * 60)
    log("TEST 3b: Create video from start + end frame")
    body = {
        "accessToken": creds["token"],
        "promptText": prompt,
        "imageUrls": [start_b64, end_b64],
        "aspectRatio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "videoModel": "VEO_3_FAST",
        "type": "frame",
    }
    r = nano_post("/api/v2/videos/create", body, nano_key)
    task_id = r.get("taskId") or r.get("id") or r.get("data", {}).get("taskId")
    if not task_id:
        raise RuntimeError(f"Không có taskId: {json.dumps(r)[:400]}")
    log(f"  ✓ Created taskId={task_id}")
    final = poll_task(task_id, nano_key, is_video=True)
    url = extract_url(final, is_video=True)
    if not url:
        raise RuntimeError(f"Không có videoUrl trong kết quả: {json.dumps(final)[:400]}")
    out_path = OUTPUT_DIR / f"test3b_video_frames_{task_id}.mp4"
    download_to(out_path, url)
    return {"task_id": task_id, "url": url, "file": str(out_path)}


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> int:
    # Force stdout UTF-8 cho Windows console (tránh UnicodeEncodeError cp1252)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="Test nano API end-to-end")
    parser.add_argument("--nano-key", required=False, default=os.environ.get("NANO_API_KEY"),
                        help="Nano API Key (Bearer). Or set env NANO_API_KEY.")
    parser.add_argument("--flask-url", default=os.environ.get("FLASK_BASE_URL", "http://localhost:8000"),
                        help="Flask URL with VEO token ingested (default http://localhost:8000)")
    parser.add_argument("--mode", choices=["image", "video-t2v", "video-i2v", "video-frames", "all"],
                        default="all", help="Test mode (default: all)")
    parser.add_argument("--prompt-image", default=TEST_PROMPT_IMAGE)
    parser.add_argument("--prompt-video", default=TEST_PROMPT_VIDEO)
    args = parser.parse_args()

    if not args.nano_key:
        print("❌ Thiếu --nano-key (hoặc env NANO_API_KEY).", file=sys.stderr)
        return 1

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    log(f"Flask URL : {args.flask_url}")
    log(f"Nano key  : {args.nano_key[:8]}...{args.nano_key[-4:]}")
    log(f"Mode      : {args.mode}")

    # 1. Pull VEO token+cookie từ Flask
    creds = get_veo_credentials(args.flask_url)

    # 2. Load test image (chỉ cần khi mode chứa I2V/frames)
    needs_image = args.mode in ("video-i2v", "video-frames", "all")
    image_b64 = None
    end_image_b64 = None
    if needs_image:
        log(f"Loading test image: {TEST_IMAGE}")
        image_b64 = file_to_base64(TEST_IMAGE)
        log(f"  → base64 len={len(image_b64)}")
        if args.mode in ("video-frames", "all"):
            log(f"Loading end image: {TEST_END_IMAGE}")
            end_image_b64 = file_to_base64(TEST_END_IMAGE)
            log(f"  → base64 len={len(end_image_b64)}")

    results: list[dict] = []
    errors: list[str] = []

    def _run(label: str, fn):
        try:
            res = fn()
            results.append({"test": label, "status": "OK", **res})
            log(f"✅ {label} OK")
        except Exception as e:
            errors.append(f"{label}: {e}")
            results.append({"test": label, "status": "FAIL", "error": str(e)})
            log(f"❌ {label} FAIL: {e}")

    if args.mode in ("image", "all"):
        _run("Test 1 - image", lambda: test_create_image(args.nano_key, creds, args.prompt_image))
    if args.mode in ("video-t2v", "all"):
        _run("Test 2 - video T2V", lambda: test_create_video_t2v(args.nano_key, creds, args.prompt_video))
    if args.mode in ("video-i2v", "all"):
        _run("Test 3a - video I2V", lambda: test_create_video_i2v(
            args.nano_key, creds, image_b64, args.prompt_video))
    if args.mode in ("video-frames", "all"):
        _run("Test 3b - video frames", lambda: test_create_video_frames(
            args.nano_key, creds, image_b64, end_image_b64, args.prompt_video))

    # ── Summary ──
    log("=" * 60)
    log("SUMMARY")
    for r in results:
        if r["status"] == "OK":
            log(f"  ✅ {r['test']:30s} → {r['file']}")
        else:
            log(f"  ❌ {r['test']:30s} → {r['error'][:200]}")
    log("=" * 60)

    return 0 if not errors else 2


if __name__ == "__main__":
    sys.exit(main())
