import os
import sys
import json

_DIR = os.path.dirname(os.path.abspath(__file__))

if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

from core import config as cfg
from core.cookie_manager import load_cookies, extract_session_token_cookie
from core.veo_client import VeoClient
from core.imagen_client import ImagenClient
from core.project import create_project

CYAN = "\033[96m"
BLUE = "\033[94m"
BOLD = "\033[1m"
RESET = "\033[0m"
DIM = "\033[2m"
RED = "\033[91m"
GREEN = "\033[92m"


def print_banner():
    print(f"\n{BOLD}{BLUE}{'=' * 60}{RESET}")
    print(f"{BOLD}{CYAN}  TAO VIDEO / ANH AI (Veo + Imagen){RESET}")
    print(f"{DIM}  Google AI Sandbox API{RESET}")
    print(f"{BOLD}{BLUE}{'=' * 60}{RESET}\n")


def select_mode():
    print(f"{CYAN}Chon che do:{RESET}")
    print(f"  {BLUE}1.{RESET} Text -> Video (Veo)")
    print(f"  {BLUE}2.{RESET} Image -> Video (Veo)")
    print(f"  {BLUE}3.{RESET} Text -> Image (Imagen)")
    print(f"  {BLUE}0.{RESET} Thoat")
    choice = input(f"\n{CYAN}-> Chon (0-3): {RESET}").strip()
    return choice


def get_cookies():
    cookies = load_cookies()
    if not cookies:
        print(
            f"{RED}[FAIL] Chua co cookie! Vui long import cookie vao cookies.json{RESET}"
        )
        return None
    cookie = extract_session_token_cookie(cookies)
    if not cookie:
        print(f"{RED}[FAIL] Khong tim thay session token trong cookies!{RESET}")
        return None
    print(f"{GREEN}[OK] Da load cookie thanh cong{RESET}")
    return cookie


def run_text_to_video(cookie):
    prompt = input(f"\n{CYAN}Nhap prompt: {RESET}").strip()
    if not prompt:
        print(f"{RED}[FAIL] Prompt trong!{RESET}")
        return

    aspect = (
        input(f"{CYAN}Ti le (1=9:16 doc, 2=16:9 ngang) [1]: {RESET}").strip() or "1"
    )
    aspect_ratio = (
        "VIDEO_ASPECT_RATIO_9_16" if aspect == "1" else "VIDEO_ASPECT_RATIO_16_9"
    )

    count_input = input(f"{CYAN}So video tao [1]: {RESET}").strip() or "1"
    count = int(count_input)

    from core.paths import ROOT_DIR
    out_folder = os.path.join(ROOT_DIR, "outputs", "veo")
    os.makedirs(out_folder, exist_ok=True)

    print(f"\n{BLUE}Dang tao {count} video...{RESET}")
    print(f"{DIM}  Prompt: {prompt[:80]}{RESET}")
    print(f"{DIM}  Aspect: {aspect_ratio}{RESET}")

    try:
        client = VeoClient(cookie)

        print(f"\n{BLUE}Tao project...{RESET}")
        project_id = create_project("AutoVoice CLI", cookie=cookie)
        if not project_id:
            print(f"{RED}[FAIL] Khong tao duoc project!{RESET}")
            return
        print(f"{DIM}  Project ID: {project_id}{RESET}")

        print(f"{BLUE}Dung shared browser runtime captcha...{RESET}")
        token = "BROWSER_RUNTIME_RECAPTCHA_IN_PAGE"
        if not token:
            print(f"{RED}[FAIL] Khong giai duoc captcha!{RESET}")
            return
        print(f"{GREEN}[OK] Captcha OK{RESET}")

        print(f"{BLUE}Gui yeu cau tao video...{RESET}")
        result = client.create_video_t2v(
            row=0,
            prompt=prompt,
            project_id=project_id,
            captcha_token=token,
            aspect=aspect_ratio,
            count=count,
        )
        if result:
            print(f"\n{GREEN}[OK] Da gui yeu cau!{RESET}")
            print(f"{DIM}{json.dumps(result, indent=2)[:500]}{RESET}")
        else:
            print(f"{RED}[FAIL] Tao video that bai!{RESET}")
        print(f"\n{DIM}Video dang render. Kiem tra ket qua tai labs.google{RESET}")

    except Exception as e:
        print(f"\n{RED}[ERROR] {e}{RESET}")


def run_image_to_video(cookie):
    image_path = input(f"\n{CYAN}Duong dan file anh bat dau: {RESET}").strip()
    if not image_path or not os.path.exists(image_path):
        print(f"{RED}[FAIL] File anh khong ton tai!{RESET}")
        return

    end_image_path = input(
        f"{CYAN}Duong dan file anh ket thuc (Enter de bo qua): {RESET}"
    ).strip()
    if end_image_path and not os.path.exists(end_image_path):
        print(f"{RED}[FAIL] File anh ket thuc khong ton tai!{RESET}")
        return

    prompt = input(f"{CYAN}Nhap prompt mo ta video: {RESET}").strip()
    if not prompt:
        print(f"{RED}[FAIL] Prompt trong!{RESET}")
        return

    aspect = (
        input(f"{CYAN}Ti le (1=9:16 doc, 2=16:9 ngang) [1]: {RESET}").strip() or "1"
    )
    aspect_ratio = (
        "VIDEO_ASPECT_RATIO_9_16" if aspect == "1" else "VIDEO_ASPECT_RATIO_16_9"
    )

    try:
        client = VeoClient(cookie)

        print(f"{BLUE}Tao project...{RESET}")
        project_id = create_project("AutoVoice CLI", cookie=cookie)
        if not project_id:
            print(f"{RED}[FAIL] Khong tao duoc project!{RESET}")
            return

        print(f"\n{BLUE}Upload anh bat dau...{RESET}")
        start_image_id = client.upload_image_from_path(
            image_path,
            project_id=project_id,
        )
        if not start_image_id:
            print(f"{RED}[FAIL] Upload anh bat dau that bai!{RESET}")
            return
        print(f"{GREEN}[OK] Start image ID: {start_image_id[:40]}...{RESET}")

        end_image_id = None
        if end_image_path:
            print(f"{BLUE}Upload anh ket thuc...{RESET}")
            end_image_id = client.upload_image_from_path(
                end_image_path,
                project_id=project_id,
            )
            if not end_image_id:
                print(f"{RED}[FAIL] Upload anh ket thuc that bai!{RESET}")
                return
            print(f"{GREEN}[OK] End image ID: {end_image_id[:40]}...{RESET}")

        print(f"{BLUE}Dung shared browser runtime captcha...{RESET}")
        token = "BROWSER_RUNTIME_RECAPTCHA_IN_PAGE"
        if not token:
            print(f"{RED}[FAIL] Khong giai duoc captcha!{RESET}")
            return
        print(f"{GREEN}[OK] Captcha OK{RESET}")

        print(f"{BLUE}Gui yeu cau tao video tu anh...{RESET}")
        if end_image_id:
            result = client.create_video_start_end_image(
                row=0,
                prompt=prompt,
                project_id=project_id,
                captcha_token=token,
                start_image_media_id=start_image_id,
                end_image_media_id=end_image_id,
                aspect=aspect_ratio,
            )
        else:
            result = client.create_video_i2v(
                row=0,
                prompt=prompt,
                project_id=project_id,
                captcha_token=token,
                start_image_media_id=start_image_id,
                aspect=aspect_ratio,
            )
        if result:
            print(f"\n{GREEN}[OK] Da gui yeu cau!{RESET}")
            print(f"{DIM}{json.dumps(result, indent=2)[:500]}{RESET}")
        else:
            print(f"{RED}[FAIL] Tao video that bai!{RESET}")
        print(f"\n{DIM}Video dang render. Kiem tra ket qua tai labs.google{RESET}")

    except Exception as e:
        print(f"\n{RED}[ERROR] {e}{RESET}")


def run_text_to_image(cookie):
    prompt = input(f"\n{CYAN}Nhap prompt: {RESET}").strip()
    if not prompt:
        print(f"{RED}[FAIL] Prompt trong!{RESET}")
        return

    aspect = input(f"{CYAN}Ti le (1=Ngang, 2=Doc, 3=Vuong) [1]: {RESET}").strip() or "1"
    aspect_map = {
        "1": "IMAGE_ASPECT_RATIO_LANDSCAPE",
        "2": "IMAGE_ASPECT_RATIO_PORTRAIT",
        "3": "IMAGE_ASPECT_RATIO_SQUARE",
    }
    aspect_ratio = aspect_map.get(aspect, "IMAGE_ASPECT_RATIO_LANDSCAPE")

    from core.paths import ROOT_DIR
    out_folder = os.path.join(ROOT_DIR, "outputs", "imagen")
    os.makedirs(out_folder, exist_ok=True)

    print(f"\n{BLUE}Dang tao anh...{RESET}")
    print(f"{DIM}  Prompt: {prompt[:80]}{RESET}")

    try:
        from core.banana_runtime.scheduler import BananaJob, BananaScheduler
        client = ImagenClient(cookie)
        veo = VeoClient(cookie)

        print(f"{BLUE}Tao project...{RESET}")
        project_id = create_project("AutoVoice CLI", cookie=cookie, tool_name="PINHOLE")
        if not project_id:
            print(f"{RED}[FAIL] Khong tao duoc project!{RESET}")
            return

        print(f"{BLUE}Gui yeu cau tao anh qua global Banana runtime...{RESET}")
        if not getattr(veo, "access_token", None):
            print(f"{RED}[FAIL] Khong lay duoc access token!{RESET}")
            return
        out_path = os.path.join(out_folder, f"imagen_cli_{int(__import__('time').time())}.png")
        scheduler = BananaScheduler(tokens=[veo.access_token], thread_count=1, max_attempts=5)
        results = scheduler.submit([
            BananaJob(
                prompt=prompt,
                model="NARWHAL",
                aspect_ratio={
                    "IMAGE_ASPECT_RATIO_LANDSCAPE": "16:9",
                    "IMAGE_ASPECT_RATIO_SQUARE": "1:1",
                }.get(aspect_ratio, "9:16"),
                output_path=out_path,
                job_id="cli-image-1",
            )
        ])
        result = [r for r in results if r.status == "completed"]
        if result:
            print(f"\n{GREEN}[OK] Da gui yeu cau!{RESET}")
            print(f"{DIM}{json.dumps(result, indent=2)[:500]}{RESET}")
        else:
            print(f"{RED}[FAIL] Tao anh that bai!{RESET}")

    except Exception as e:
        print(f"\n{RED}[ERROR] {e}{RESET}")


def main():
    print_banner()

    cookie = get_cookies()
    if not cookie:
        return

    while True:
        choice = select_mode()
        if choice == "0":
            print(f"\n{CYAN}Tam biet!{RESET}")
            break
        elif choice == "1":
            run_text_to_video(cookie)
        elif choice == "2":
            run_image_to_video(cookie)
        elif choice == "3":
            run_text_to_image(cookie)
        else:
            print(f"{RED}[FAIL] Lua chon khong hop le!{RESET}")


if __name__ == "__main__":
    main()
