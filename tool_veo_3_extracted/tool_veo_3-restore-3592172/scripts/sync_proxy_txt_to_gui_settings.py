"""Sync proxies from proxy.txt into reference-implementation/gui_settings.json.

This helper is intentionally standalone so the existing app/scheduler flow is not changed.
The app still reads proxies from gui_settings.json as before.

Usage:
  python scripts/sync_proxy_txt_to_gui_settings.py
  python scripts/sync_proxy_txt_to_gui_settings.py --proxy-file proxy.txt
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_PROXY_FILE = ROOT_DIR / "proxy.txt"
DEFAULT_SETTINGS_FILE = ROOT_DIR / "reference-implementation" / "gui_settings.json"


def read_proxy_lines(proxy_file: Path) -> list[str]:
    if not proxy_file.exists():
        raise FileNotFoundError(f"Proxy file not found: {proxy_file}")
    return [line.strip() for line in proxy_file.read_text(encoding="utf-8").splitlines() if line.strip()]


def sync_proxies(proxy_file: Path, settings_file: Path) -> int:
    if settings_file.exists():
        data = json.loads(settings_file.read_text(encoding="utf-8"))
    else:
        data = {}

    proxies = read_proxy_lines(proxy_file)
    data["proxies"] = "\n".join(proxies)

    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(proxies)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync proxy.txt into GUI settings without changing app code")
    parser.add_argument("--proxy-file", default=str(DEFAULT_PROXY_FILE), help="Path to proxy.txt")
    parser.add_argument("--settings-file", default=str(DEFAULT_SETTINGS_FILE), help="Path to gui_settings.json")
    args = parser.parse_args()

    proxy_file = Path(args.proxy_file)
    settings_file = Path(args.settings_file)
    count = sync_proxies(proxy_file, settings_file)
    print(f"Synced {count} proxies")
    print(f"From: {proxy_file}")
    print(f"To:   {settings_file}")


if __name__ == "__main__":
    main()
