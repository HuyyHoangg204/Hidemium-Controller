"""
Watch UI source files and auto-rebuild dist/index.html on change.
Usage: python watch.py
"""
import subprocess, sys, time, os
from pathlib import Path

WATCH_DIR = Path(__file__).parent / 'src'
BUILD_SCRIPT = Path(__file__).parent / 'do_build.py'

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
except ImportError:
    print("Installing watchdog...")
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'watchdog', '-q'])
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler


class BuildHandler(FileSystemEventHandler):
    def __init__(self):
        self._last_build = 0

    def on_modified(self, event):
        if event.is_directory:
            return
        if not event.src_path.endswith(('.html', '.css')):
            return
        # Debounce: ignore duplicate events within 500ms
        now = time.time()
        if now - self._last_build < 0.5:
            return
        self._last_build = now
        fname = os.path.basename(event.src_path)
        print(f"\n[CHANGE] {fname} → rebuilding...", flush=True)
        try:
            result = subprocess.run(
                [sys.executable, str(BUILD_SCRIPT)],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                print(f"[OK] Build thành công!", flush=True)
            else:
                print(f"[ERROR] Build thất bại:\n{result.stderr}", flush=True)
        except Exception as e:
            print(f"[ERROR] {e}", flush=True)


if __name__ == '__main__':
    print(f"👀 Đang theo dõi: {WATCH_DIR}")
    print(f"🔨 Script build:  {BUILD_SCRIPT}")
    print("Nhấn Ctrl+C để dừng.\n")

    observer = Observer()
    observer.schedule(BuildHandler(), str(WATCH_DIR), recursive=True)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
    print("Đã dừng.")
