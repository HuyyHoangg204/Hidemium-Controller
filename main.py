from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from app_logging import setup_logging
from hidemium_client import HidemiumClient, HidemiumError, pretty


def load_env() -> None:
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def parse_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    path = Path(value)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return json.loads(value)


def default_is_local() -> bool:
    return os.getenv("HIDEMIUM_IS_LOCAL", "false").lower() in {"1", "true", "yes", "y"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hidemiumctl", description="Điều khiển Hidemium API Automation V4")
    parser.add_argument("--base-url", default=os.getenv("HIDEMIUM_BASE_URL", "http://127.0.0.1:2222"))
    parser.add_argument("--timeout", type=int, default=int(os.getenv("REQUEST_TIMEOUT", "30")))

    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("user-uuid", help="Lấy user uuid/token")
    sub.add_parser("versions", help="Lấy danh sách browser version")
    sub.add_parser("statuses", help="Lấy danh sách status")
    sub.add_parser("tags", help="Lấy danh sách tag")
    sub.add_parser("configs", help="Lấy danh sách default config")

    p = sub.add_parser("list", help="Liệt kê profile")
    p.add_argument("--local", action="store_true", default=default_is_local())
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--search", default="")

    p = sub.add_parser("get", help="Lấy profile theo uuid")
    p.add_argument("uuid")
    p.add_argument("--local", action="store_true", default=default_is_local())

    p = sub.add_parser("open", help="Mở profile")
    p.add_argument("uuid")
    p.add_argument("--command", default="--window-position=100,100 --window-size=1280,800")
    p.add_argument("--proxy", default="", help="Ví dụ: HTTP|host|port|user|pass")

    p = sub.add_parser("close", help="Đóng profile")
    p.add_argument("uuid")

    p = sub.add_parser("authorize", help="Check authorize profile")
    p.add_argument("uuid")

    p = sub.add_parser("create-default", help="Tạo profile bằng default config; --json nhận JSON string hoặc đường dẫn file")
    p.add_argument("--json", required=True)
    p.add_argument("--local", action="store_true", default=default_is_local())

    p = sub.add_parser("create-custom", help="Tạo profile custom; --json nhận JSON string hoặc đường dẫn file")
    p.add_argument("--json", required=True)
    p.add_argument("--local", action="store_true", default=True)

    p = sub.add_parser("delete", help="Xóa profile theo id/uuid")
    p.add_argument("ids", nargs="+")
    p.add_argument("--local", action="store_true", default=default_is_local())

    p = sub.add_parser("proxy-remove", help="Gỡ proxy khỏi profile")
    p.add_argument("browser_uuid")
    p.add_argument("--local", action="store_true", default=default_is_local())

    p = sub.add_parser("rename", help="Sửa tên profile")
    p.add_argument("uuid")
    p.add_argument("name")
    p.add_argument("--local", action="store_true", default=default_is_local())

    p = sub.add_parser("note", help="Sửa ghi chú profile")
    p.add_argument("uuid")
    p.add_argument("note")
    p.add_argument("--local", action="store_true", default=default_is_local())

    p = sub.add_parser("status", help="Đổi status profile")
    p.add_argument("uuid")
    p.add_argument("status_id", type=int)
    p.add_argument("--local", action="store_true", default=default_is_local())

    p = sub.add_parser("tags-set", help="Gán tag cho profile, cách nhau bằng dấu phẩy")
    p.add_argument("uuid")
    p.add_argument("tags")
    p.add_argument("--local", action="store_true", default=default_is_local())

    p = sub.add_parser("campaigns", help="List campaign")
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--search", default="")

    p = sub.add_parser("campaign-create", help="Tạo campaign từ JSON string/file")
    p.add_argument("--json", required=True)

    p = sub.add_parser("campaign-delete", help="Xóa campaign")
    p.add_argument("ids", nargs="+", type=int)

    p = sub.add_parser("schedules", help="List schedule theo campaign")
    p.add_argument("campaign_id", type=int)
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("schedule-create", help="Tạo schedule từ JSON string/file")
    p.add_argument("--json", required=True)

    p = sub.add_parser("schedule-status", help="Bật/tắt schedule")
    p.add_argument("schedule_id", type=int)
    p.add_argument("enabled", choices=["true", "false", "1", "0", "on", "off"])

    p = sub.add_parser("schedule-delete", help="Xóa schedule")
    p.add_argument("ids", nargs="+", type=int)

    return parser


def main() -> int:
    load_env()
    logger = setup_logging("hidemium_controller.cli", console=True)
    parser = build_parser()
    args = parser.parse_args()
    logger.info("CLI command=%s base_url=%s", args.cmd, args.base_url)
    client = HidemiumClient(args.base_url, args.timeout, logger=logger)

    try:
        if args.cmd == "user-uuid":
            result = client.get_user_uuid()
        elif args.cmd == "versions":
            result = client.list_versions()
        elif args.cmd == "statuses":
            result = client.list_status(default_is_local())
        elif args.cmd == "tags":
            result = client.list_tags(default_is_local())
        elif args.cmd == "configs":
            result = client.list_default_configs()
        elif args.cmd == "list":
            result = client.list_profiles(args.local, args.page, args.limit, args.search)
        elif args.cmd == "get":
            result = client.get_profile(args.uuid, args.local)
        elif args.cmd == "open":
            result = client.open_profile(args.uuid, args.command, args.proxy)
        elif args.cmd == "close":
            result = client.close_profile(args.uuid)
        elif args.cmd == "authorize":
            result = client.check_authorize(args.uuid)
        elif args.cmd == "create-default":
            result = client.create_profile_by_default(parse_json(args.json), args.local)
        elif args.cmd == "create-custom":
            result = client.create_profile_custom(parse_json(args.json), args.local)
        elif args.cmd == "delete":
            result = client.delete_profile(args.ids, args.local)
        elif args.cmd == "proxy-remove":
            result = client.edit_proxy(args.browser_uuid, -1, args.local)
        elif args.cmd == "rename":
            result = client.update_profile_name(args.uuid, args.name, args.local)
        elif args.cmd == "note":
            result = client.update_profile_note(args.uuid, args.note, args.local)
        elif args.cmd == "status":
            result = client.change_profile_status(args.uuid, args.status_id, args.local)
        elif args.cmd == "tags-set":
            result = client.sync_tags(args.uuid, [x.strip() for x in args.tags.split(",") if x.strip()], args.local)
        elif args.cmd == "campaigns":
            result = client.list_campaigns(args.page, args.limit, args.search)
        elif args.cmd == "campaign-create":
            result = client.create_campaign(parse_json(args.json))
        elif args.cmd == "campaign-delete":
            result = client.delete_campaign(args.ids)
        elif args.cmd == "schedules":
            result = client.list_schedules(args.campaign_id, args.page, args.limit)
        elif args.cmd == "schedule-create":
            result = client.create_schedule(parse_json(args.json))
        elif args.cmd == "schedule-status":
            result = client.update_schedule_status({"id": args.schedule_id, "status": args.enabled in {"true", "1", "on"}})
        elif args.cmd == "schedule-delete":
            result = client.delete_schedule(args.ids)
        else:
            parser.error("Lệnh không hỗ trợ")
            return 2
    except (HidemiumError, json.JSONDecodeError) as exc:
        logger.exception("Command failed: %s", exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    logger.info("Command completed: %s", args.cmd)
    print(pretty(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
