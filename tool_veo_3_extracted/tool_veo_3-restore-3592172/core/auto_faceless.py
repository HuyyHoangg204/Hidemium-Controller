"""
Auto Faceless Video Pipeline
-----------------------------
Biến 1 link YouTube bất kỳ thành video Infographic hoàn chỉnh:
  Step 1: yt-dlp  -> tải audio (.mp3) + subtitle (.srt)
  Step 2: SRT     -> gom nhóm câu thành các Scene (5-8s)
  Step 3: LLM     -> tạo prompt ảnh infographic cho từng Scene
  Step 4: Veo 3   -> sinh video clip 8s/scene
  Step 5: MoviePy -> căn chỉnh clip khớp audio
  Step 6: MoviePy -> Master render (audio + clips + subtitle)
"""

import os
import re
import json
import time
import sys

_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

# ---------------------------------------------------------------------------
# STEP 1 : Download YouTube Assets
# ---------------------------------------------------------------------------


def download_youtube_assets(url: str, output_dir: str = "temp"):
    """Tải audio (.mp3) và subtitle (.srt) từ YouTube bằng yt-dlp."""
    import yt_dlp

    os.makedirs(output_dir, exist_ok=True)

    # ====== PASS 1: Tải Audio ======
    audio_opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(output_dir, "audio.%(ext)s"),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
        "quiet": True,
        "no_warnings": True,
    }

    title = "video"
    with yt_dlp.YoutubeDL(audio_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        title = info.get("title", "video")

    # ====== PASS 2: Tải Subtitle (riêng biệt, tránh 429) ======
    # Thử từng ngôn ngữ một, ưu tiên en -> vi
    srt_file = None
    for lang in ["en", "vi", "en-US", "en-GB"]:
        if srt_file:
            break
        sub_opts = {
            "skip_download": True,  # Không tải lại video/audio
            "writesubtitles": True,  # Sub do creator upload
            "writeautomaticsub": True,  # Sub tự động (fallback)
            "subtitleslangs": [lang],  # Chỉ 1 ngôn ngữ mỗi lần
            "subtitlesformat": "srt/best",
            "outtmpl": os.path.join(output_dir, "subtitle.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
        }
        try:
            with yt_dlp.YoutubeDL(sub_opts) as ydl:
                ydl.download([url])
            # Tìm file SRT/VTT vừa tải
            for f in os.listdir(output_dir):
                if f.startswith("subtitle") and (
                    f.endswith(".srt") or f.endswith(".vtt")
                ):
                    srt_file = os.path.join(output_dir, f)
                    break
        except Exception:
            pass
        time.sleep(1)  # Nghỉ 1s giữa các lần thử tránh 429

    # Fallback: tìm bất kỳ file sub nào trong thư mục
    if not srt_file:
        for f in os.listdir(output_dir):
            if f.endswith(".srt") or f.endswith(".vtt"):
                srt_file = os.path.join(output_dir, f)
                break

    audio_file = os.path.join(output_dir, "audio.mp3")
    if not os.path.exists(audio_file):
        for f in os.listdir(output_dir):
            if f.startswith("audio.") and not f.endswith(".srt"):
                audio_file = os.path.join(output_dir, f)
                break

    return {
        "title": title,
        "audio": audio_file,
        "srt": srt_file,
        "output_dir": output_dir,
    }


# ---------------------------------------------------------------------------
# STEP 2 : Parse SRT & Chunk into ~8s Scenes
# ---------------------------------------------------------------------------


def parse_srt(srt_path: str):
    """Đọc file .srt và trả về danh sách các dòng sub {start, end, text}."""
    entries = []
    with open(srt_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    # Regex khớp cả SRT và VTT
    pattern = re.compile(
        r"(\d{2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[.,]\d{3})\s*\n((?:(?!\d+\n\d{2}:\d{2}).+\n?)+)",
        re.MULTILINE,
    )

    for m in pattern.finditer(content):
        start_str = m.group(1).replace(",", ".")
        end_str = m.group(2).replace(",", ".")
        text = m.group(3).strip()
        text = re.sub(r"<[^>]+>", "", text)  # Xóa tag HTML
        text = text.replace("\n", " ")

        entries.append(
            {
                "start": _ts_to_seconds(start_str),
                "end": _ts_to_seconds(end_str),
                "text": text,
            }
        )

    return entries


def _ts_to_seconds(ts: str) -> float:
    """Chuyển '00:01:23.456' thành giây (float)."""
    parts = ts.split(":")
    h, m = int(parts[0]), int(parts[1])
    s = float(parts[2])
    return h * 3600 + m * 60 + s


def chunk_scenes(entries: list, target_duration: float = 8.0):
    """
    Gom nhóm các dòng SRT liên tiếp thành các Scene.
    Mỗi Scene cố gắng dài ~target_duration giây.
    Timestamp liên tục: Scene 1: 0-8s, Scene 2: 8-16s, ...
    """
    if not entries:
        return []

    scenes = []
    current_texts = []
    scene_start = entries[0]["start"]
    cursor = 0.0  # Thời điểm bắt đầu liên tục

    for entry in entries:
        current_texts.append(entry["text"])
        elapsed = entry["end"] - scene_start

        if elapsed >= target_duration:
            dur = round(entry["end"] - scene_start, 1)
            scenes.append(
                {
                    "index": len(scenes),
                    "start": round(cursor, 1),
                    "end": round(cursor + dur, 1),
                    "duration": dur,
                    "text": " ".join(current_texts),
                }
            )
            cursor += dur
            current_texts = []
            scene_start = entry["end"]

    # Phần còn lại
    if current_texts and entries:
        last = entries[-1]
        dur = round(last["end"] - scene_start, 1)
        if dur > 0:
            scenes.append(
                {
                    "index": len(scenes),
                    "start": round(cursor, 1),
                    "end": round(cursor + dur, 1),
                    "duration": dur,
                    "text": " ".join(current_texts),
                }
            )

    return scenes


# ---------------------------------------------------------------------------
# STEP 3 : LLM Prompt Generation (Groq API - Free, Fast, No Limit Issues)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a professional art director for infographic-style explainer videos.
Read the narration script below and write ONE short English image prompt describing a SAFE, FAMILY-FRIENDLY visual scene.

STRICT RULES:
- Describe the main subject using ABSTRACT, METAPHORICAL, POSITIVE imagery only.
- NEVER use ANY of these words or similar: dark, illegal, murder, kill, drug, weapon, blood, violence, crime, hack, steal, death, scary, horror, shadow, lurk, danger, substances, vendor, dealer, alley, gun, knife, war, attack, abuse, exploit, threat, scam, fraud, naked, nude, sex, porn, torture, suicide, bomb, terror, corrupt, toxic, victim, predator, sinister, menacing, creepy, disturbing, grotesque, gruesome.
- Replace negative concepts with neutral alternatives:
  "internet security" NOT "hacking"
  "digital landscape" NOT "dark web"
  "online marketplace" NOT "illegal market"
  "person researching on computer" NOT "exploring dark web"
  "financial transaction" NOT "money laundering"
  "community discussion" NOT "underground forum"
- ALWAYS end with: , flat vector illustration, corporate memphis style, clean pastel background, static wide camera angle, 8k resolution, infographic style, no text on image
- Keep under 50 words (excluding the suffix).
- Output ONLY the image prompt. No quotes, no numbering, no explanation.
- Use simple English words only. No special characters like quotes, brackets, or unicode."""

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"


def generate_prompts_groq(scenes: list, api_key: str):
    """
    Dùng Groq API (miễn phí, siêu nhanh) để sinh prompt ảnh cho từng Scene.
    Groq chạy Llama 3 70B, rate limit rất thoải mái.
    Không cần cài thêm thư viện - chỉ dùng requests.
    """
    import requests

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    prompts = []
    for i, scene in enumerate(scenes):
        try:
            payload = {
                "model": "llama-3.3-70b-versatile",
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f'Narration: "{scene["text"]}"'},
                ],
                "temperature": 0.7,
                "max_tokens": 200,
            }

            resp = requests.post(
                GROQ_API_URL, headers=headers, json=payload, timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
            prompt_text = data["choices"][0]["message"]["content"].strip()
            prompts.append(prompt_text)
            print(f"  [Scene {i+1}/{len(scenes)}] Prompt: {prompt_text[:80]}...")
        except Exception as e:
            print(f"  [Scene {i+1}] Lỗi Groq: {e}")
            # Fallback: dùng chính text làm prompt
            prompts.append(
                f"{scene['text']}, flat vector illustration, corporate memphis style, "
                "clean pastel background, static wide camera angle, 8k resolution, infographic style, no text on image"
            )
        time.sleep(0.3)  # Groq cho phép thoải mái, chỉ cần nghỉ nhẹ

    return prompts


# ---------------------------------------------------------------------------
# STEP 4 : Generate Video Clips - dùng lại chính xác logic VeoWorker
# ---------------------------------------------------------------------------

POLL_INTERVAL = 8
POLL_MAX_WAIT = 600


def _sanitize_prompt(prompt: str) -> str:
    """Làm sạch prompt LLM trước khi gửi Veo API."""
    prompt = prompt.strip().strip("\"'`")
    # Bỏ markdown formatting
    prompt = re.sub(r"\*{1,2}(.+?)\*{1,2}", r"\1", prompt)
    prompt = re.sub(r"#{1,6}\s*", "", prompt)
    prompt = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", prompt)
    # Bỏ newlines, tabs, double spaces
    prompt = prompt.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    prompt = re.sub(r"\s{2,}", " ", prompt)
    # Chỉ giữ ASCII printable
    prompt = re.sub(r"[^\x20-\x7E]", "", prompt)
    # Giới hạn 500 ký tự
    if len(prompt) > 500:
        prompt = prompt[:500]
    return prompt.strip()


def generate_veo_clips(
    prompts: list,
    scenes: list,
    output_dir: str,
    session_token: str,
    aspect: str = "landscape",
):
    """
    Tạo video clip bằng Veo 3 - copy chính xác logic từ VeoWorker.run().
    """
    import json as _json
    import urllib.request
    from core.veo_client import VeoClient
    from core.project import create_project

    # --- Sao chép hàm helper từ veo_worker.py ---
    def _extract_status_local(poll_resp):
        try:
            op_obj = poll_resp.get("operations", [{}])[0]
            for mg in op_obj.get("mediaGenerations", []):
                st = mg.get("mediaGenerationStatus", "")
                if st:
                    return st
            if op_obj.get("done"):
                return "MEDIA_GENERATION_STATUS_COMPLETE"
        except Exception:
            pass
        return "UNKNOWN"

    def _extract_video_url_local(poll_resp):
        try:
            for op in poll_resp.get("operations", []):
                for mg in op.get("mediaGenerations", []):
                    for v in mg.get("videos", []):
                        url = v.get("encodedVideoUri") or v.get("uri")
                        if url:
                            return url
        except Exception:
            pass
        return None

    def _download_video_local(url, dest_path, headers=None):
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req) as resp, open(dest_path, "wb") as out:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                out.write(chunk)

    # --- Bắt đầu logic giống hệt VeoWorker.run() ---

    clips_dir = os.path.join(output_dir, "clips")
    os.makedirs(clips_dir, exist_ok=True)

    # 1. Auth  (giống VeoWorker dòng 141)
    client = VeoClient(session_token)

    # 2. Aspect ratio  (giống VeoWorker dòng 145-150)
    aspect_ratio = (
        "VIDEO_ASPECT_RATIO_16_9"
        if aspect == "landscape"
        else "VIDEO_ASPECT_RATIO_9_16"
    )

    # 3. Tạo project  (giống VeoWorker dòng 153-162)
    project_id = create_project(
        "AutoFaceless",
        tool_name="PINHOLE",
        cookie=session_token,
        access_token=client.access_token,
    )
    print(f"  [Faceless] project_id={project_id!r}")
    if not project_id:
        print("  [!] Không thể tạo project!")
        return [None] * len(prompts)

    clip_paths = []

    for i, prompt in enumerate(prompts):
        row = i  # row giống VeoWorker
        print(f"  [Clip {i+1}/{len(prompts)}] Đang tạo video Veo 3...")
        try:
            # 4. Captcha: shared browser runtime performs in-page recaptcha.
            token = "BROWSER_RUNTIME_RECAPTCHA_IN_PAGE"

            # 5. Create video T2V  (giống VeoWorker dòng 224-231)
            clean_prompt = _sanitize_prompt(prompt)
            print(f"    [PROMPT] {clean_prompt[:120]}...")

            result = client.create_video_t2v(
                row=row,
                prompt=clean_prompt,
                project_id=project_id,
                captcha_token=token,
                aspect=aspect_ratio,
                count=1,
            )

            if not result:
                print(f"    -> create_video_t2v thất bại!")
                clip_paths.append(None)
                continue

            # 6. Lấy ops  (giống VeoWorker dòng 237-240)
            ops = result.get("ops", [])
            if not ops:
                print(f"    -> Không có ops!")
                clip_paths.append(None)
                continue

            # 7. Poll  (giống VeoWorker dòng 244-301)
            op = ops[0]
            op_name = op.get("operation", {}).get("name", "")
            scene_id = op.get("sceneId", result.get("scene_id", ""))
            if not op_name:
                clip_paths.append(None)
                continue

            video_url = None
            elapsed = 0
            current_status = "MEDIA_GENERATION_STATUS_PENDING"
            prev_st = None

            while elapsed < POLL_MAX_WAIT:
                time.sleep(POLL_INTERVAL)
                elapsed += POLL_INTERVAL

                poll_resp = client.check_status_batch(
                    row, op_name, scene_id, current_status
                )
                if not poll_resp:
                    continue

                st = _extract_status_local(poll_resp)
                current_status = st

                if st != prev_st:
                    print(f"    poll elapsed={elapsed}s status={st}")
                    prev_st = st
                else:
                    print(f"    poll elapsed={elapsed}s status={st}")

                if "ACTIVE" in st or "PENDING" in st or st == "UNKNOWN":
                    continue

                video_url = _extract_video_url_local(poll_resp)

                if "COMPLETE" in st or "SUCCESS" in st or "DONE" in st:
                    if not video_url:
                        print(f"    COMPLETE but no URL!")
                    break
                elif "FAIL" in st or "ERROR" in st or "CANCEL" in st:
                    print(f"    terminal status={st}")
                    break
                else:
                    if video_url:
                        print(f"    unknown status={st} but URL found")
                        break

            # 8. Download  (giống VeoWorker dòng 303-323)
            if video_url:
                clip_path = os.path.join(clips_dir, f"scene_{i:03d}.mp4")
                dl_headers = {"User-Agent": client.base_headers.get("User-Agent", "")}
                try:
                    _download_video_local(video_url, clip_path, headers=dl_headers)
                    print(f"    -> Tải xong: {clip_path}")
                    clip_paths.append(clip_path)
                except Exception as de:
                    print(f"    -> Download error: {de}")
                    clip_paths.append(None)
            else:
                print(f"    -> Không có video URL cho scene {i+1}")
                clip_paths.append(None)

        except Exception as e:
            print(f"    -> Lỗi Veo 3: {e}")
            clip_paths.append(None)

        time.sleep(2)

    return clip_paths


# ---------------------------------------------------------------------------
# STEP 5 & 6 : Align Clips + Master Render (FFmpeg)
# ---------------------------------------------------------------------------


def _run_ffmpeg(args, tag="ffmpeg"):
    """Chạy lệnh ffmpeg, ẩn console trên Windows."""
    import subprocess

    si = None
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    print(f"  [{tag}] {' '.join(args[:6])}...")
    result = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        startupinfo=si,
    )
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="ignore")[-500:]
        print(f"  [{tag}] stderr: {err}")
    return result.returncode == 0


def render_final_video(
    scenes: list,
    clip_paths: list,
    audio_path: str,
    srt_path: str,
    output_path: str = "output_faceless.mp4",
):
    """
    Dùng FFmpeg để:
      1. Trim/loop mỗi clip cho khớp thời lượng scene
      2. Nối tất cả clip lại
      3. Gắn audio gốc
      4. Burn subtitle
    """
    import subprocess

    clips_dir = os.path.dirname(output_path) or "."
    tmp_dir = os.path.join(clips_dir, "_render_tmp")
    os.makedirs(tmp_dir, exist_ok=True)

    aligned_files = []
    valid_count = 0

    for i, (scene, clip_path) in enumerate(zip(scenes, clip_paths)):
        target_dur = scene["duration"]
        out_clip = os.path.join(tmp_dir, f"aligned_{i:03d}.mp4")

        if clip_path and os.path.exists(clip_path):
            # Trim hoặc loop clip cho khớp target_dur
            ok = _run_ffmpeg(
                [
                    "ffmpeg",
                    "-y",
                    "-stream_loop",
                    "-1",  # Loop vô hạn (sẽ bị cắt bởi -t)
                    "-i",
                    clip_path,
                    "-t",
                    str(target_dur),  # Cắt đúng thời lượng
                    "-c:v",
                    "libx264",
                    "-an",  # Bỏ audio gốc của clip
                    "-preset",
                    "fast",
                    "-pix_fmt",
                    "yuv420p",
                    out_clip,
                ],
                tag=f"trim_{i}",
            )
            if ok:
                aligned_files.append(out_clip)
                valid_count += 1
                continue

        # Fallback: tạo video đen nếu clip lỗi
        _run_ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c=0x14141E:s=1920x1080:d={target_dur}:r=30",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-preset",
                "fast",
                out_clip,
            ],
            tag=f"black_{i}",
        )
        aligned_files.append(out_clip)

    if not aligned_files:
        print("[!] Không có clip nào để render!")
        return None

    # Tạo file list cho FFmpeg concat
    list_file = os.path.join(tmp_dir, "concat_list.txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for fp in aligned_files:
            # FFmpeg concat cần đường dẫn tuyệt đối với escape
            abs_path = os.path.abspath(fp).replace("\\", "/")
            f.write(f"file '{abs_path}'\n")

    # Bước 1: Nối tất cả clip (không audio)
    concat_video = os.path.join(tmp_dir, "concat_no_audio.mp4")
    ok = _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            list_file,
            "-c",
            "copy",
            concat_video,
        ],
        tag="concat",
    )
    if not ok:
        print("[!] Nối video thất bại!")
        return None

    # Bước 2: Gắn audio gốc + burn subtitle
    ffmpeg_cmd = [
        "ffmpeg",
        "-y",
        "-i",
        concat_video,
        "-i",
        audio_path,
        "-c:v",
        "libx264",
        "-c:a",
        "aac",
        "-preset",
        "medium",
        "-shortest",  # Dừng khi stream ngắn hơn kết thúc
        "-pix_fmt",
        "yuv420p",
    ]

    # Burn subtitle nếu có
    srt_abs = os.path.abspath(srt_path).replace("\\", "/").replace(":", "\\:")
    ffmpeg_cmd += [
        "-vf",
        f"subtitles='{srt_abs}':force_style='FontSize=22,PrimaryColour=&H00FFFFFF'",
    ]

    ffmpeg_cmd.append(output_path)

    print(f"[*] Đang render video cuối cùng: {output_path}")
    ok = _run_ffmpeg(ffmpeg_cmd, tag="render")

    # Cleanup tmp
    try:
        import shutil

        shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception:
        pass

    if ok:
        print(f"[+] HOÀN TẤT! Video xuất tại: {output_path}")
        return output_path
    else:
        print("[!] Render thất bại!")
        return None


# ---------------------------------------------------------------------------
# FULL PIPELINE RUNNER
# ---------------------------------------------------------------------------


def run_pipeline(
    youtube_url: str,
    gemini_api_key: str,
    session_token: str,
    output_dir: str = "temp",
    output_file: str = "output_faceless.mp4",
    on_status=None,
):
    """
    Chạy toàn bộ 6 bước.
    on_status(step, message) được gọi để cập nhật trạng thái lên UI.
    """

    def _log(step, msg):
        print(f"[Step {step}] {msg}")
        if on_status:
            on_status(step, msg)

    # -- STEP 1 --
    _log(1, "Đang tải audio và phụ đề từ YouTube...")
    assets = download_youtube_assets(youtube_url, output_dir)
    if not assets["srt"]:
        _log(1, "THẤT BẠI: Không tìm thấy file phụ đề!")
        return None
    _log(1, f"Tải xong: {assets['audio']}, {assets['srt']}")

    # -- STEP 2 --
    _log(2, "Đang phân tích file SRT và gom nhóm cảnh...")
    entries = parse_srt(assets["srt"])
    scenes = chunk_scenes(entries, target_duration=8.0)
    _log(2, f"Gom được {len(scenes)} cảnh từ {len(entries)} dòng sub.")

    # -- STEP 3 --
    _log(3, "Đang dùng AI tạo prompt cho từng cảnh...")
    prompts = generate_prompts_gemini(scenes, gemini_api_key)
    _log(3, f"Đã tạo {len(prompts)} prompt.")

    # -- STEP 4 --
    _log(4, "Đang tạo video clip bằng Veo 3...")
    clip_paths = generate_veo_clips(prompts, scenes, output_dir, session_token)
    success = sum(1 for p in clip_paths if p)
    _log(4, f"Tạo xong {success}/{len(clip_paths)} clip.")

    # -- STEP 5 & 6 --
    _log(5, "Đang cắt ghép và render video cuối cùng...")
    result = render_final_video(
        scenes,
        clip_paths,
        assets["audio"],
        assets["srt"],
        output_path=os.path.join(output_dir, output_file),
    )
    if result:
        _log(6, f"HOÀN TẤT! Video: {result}")
    else:
        _log(6, "Render thất bại.")

    return result


if __name__ == "__main__":
    print("=== AUTO FACELESS VIDEO PIPELINE ===")
    print("File này chứa các hàm core. Chạy từ giao diện app.py hoặc CLI.")
