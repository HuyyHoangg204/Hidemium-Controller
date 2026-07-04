"""Small access-token smoke test for Flow image creation.

Usage:
    1. Put one ya29 access token per line in tokens_test.txt
    2. Run: python test_tokens.py

The script only prints masked tokens and writes token_test_results.json.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from banana_client import BananaImageClient


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_TOKENS_FILE = BASE_DIR / "tokens_test.txt"
DEFAULT_OUTPUT_FILE = BASE_DIR / "token_test_results.json"
DEFAULT_PROMPT = "simple test image, plain white background"
DEFAULT_ACCOUNT_SESSIONS_URL = "http://127.0.0.1:5000/api/admin/veo/account-sessions"


def mask_token(token: str) -> str:
    token = token.strip()
    if len(token) <= 18:
        return token[:4] + "..."
    return f"{token[:10]}...{token[-6:]}"


def classify_error(message: str) -> str:
    lower = message.lower()
    if "http 401" in lower or "invalid authentication credentials" in lower:
        return "HTTP_401_INVALID_AUTH"
    if "http 403" in lower or "unusual_activity" in lower or "recaptcha" in lower:
        return "HTTP_403_OR_RECAPTCHA"
    if "captcha timeout" in lower:
        return "CAPTCHA_TIMEOUT"
    if "timeout" in lower:
        return "TIMEOUT"
    return "ERROR"


def fetch_account_token_map(api_url: str, api_key: str, timeout: int = 120) -> dict[str, dict[str, Any]]:
    if not api_url or not api_key:
        return {}

    request = urllib.request.Request(
        api_url,
        headers={
            "X-API-Key": api_key,
            "Accept": "application/json",
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))

    data = payload.get("data") if isinstance(payload, dict) else payload
    items = (data or {}).get("items") if isinstance(data, dict) else []
    token_map: dict[str, dict[str, Any]] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        token = (item.get("token") or "").strip()
        if not token:
            continue
        token_map[token] = {
            "account_name": item.get("account_name") or "",
            "project_id": item.get("project_id") or "",
            "proxy": item.get("proxy") or "",
            "ok": bool(item.get("ok")),
            "token": mask_token(token),
        }
    return token_map


def read_tokens(path: Path) -> list[str]:
    if not path.exists():
        path.write_text(
            "# Paste one ya29 access token per line. Lines starting with # are ignored.\n",
            encoding="utf-8",
        )
        raise SystemExit(f"Created {path}. Paste tokens into it, then run again.")

    tokens: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        tokens.append(value)
    return tokens


def test_one_token(
    index: int,
    token: str,
    args: argparse.Namespace,
    account_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    masked = mask_token(token)
    account_name = (account_info or {}).get("account_name") or ""
    lane_id = f"token-test-{index:02d}"
    user_data_dir = str(BASE_DIR / ".token_test_profiles" / lane_id)
    logs: list[str] = []

    def logger(message: str) -> None:
        safe_message = message.replace(token, masked)
        logs.append(safe_message)
        if args.verbose:
            print(f"    {safe_message}")

    client = BananaImageClient(
        logger=logger,
        lane_id=lane_id,
        user_data_dir=user_data_dir,
        proxy=args.proxy,
    )

    started_at = time.time()
    try:
        result = client.create_image(
            access_token=token,
            prompt=args.prompt,
            aspect_ratio=args.aspect_ratio,
            model=args.model,
            project_id=args.project_id,
        )
        duration = time.time() - started_at
        status = "OK"
        summary = {
            "index": index,
            "token": masked,
            "status": status,
            "duration_seconds": round(duration, 2),
            "result_type": result.get("type"),
            "has_download": bool(result.get("downloadUrl")),
            "has_operation": bool(result.get("operationName")),
            "operation_name": result.get("operationName"),
            "scene_id": result.get("sceneId"),
            "account_name": account_name,
            "account_project_id": (account_info or {}).get("project_id") or "",
            "account_proxy": (account_info or {}).get("proxy") or "",
            "error": None,
        }
        print(
            f"[{index:02d}] {masked} status=OK "
            f"type={summary['result_type']} "
            f"download={summary['has_download']} operation={summary['has_operation']} "
            f"account={account_name or '-'} "
            f"duration={duration:.2f}s"
        )
        return summary
    except Exception as exc:
        duration = time.time() - started_at
        message = str(exc)
        status = classify_error(message)
        print(f"[{index:02d}] {masked} status={status} duration={duration:.2f}s error={message}")
        return {
            "index": index,
            "token": masked,
            "status": status,
            "duration_seconds": round(duration, 2),
            "result_type": None,
            "has_download": False,
            "has_operation": False,
            "operation_name": None,
            "scene_id": None,
            "account_name": account_name,
            "account_project_id": (account_info or {}).get("project_id") or "",
            "account_proxy": (account_info or {}).get("proxy") or "",
            "error": message,
            "logs_tail": logs[-20:],
        }
    finally:
        client.shutdown(remove_profile=args.remove_profile)


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test Flow ya29 access tokens.")
    parser.add_argument("--tokens-file", default=str(DEFAULT_TOKENS_FILE))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_FILE))
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--aspect-ratio", default="16:9", choices=["16:9", "9:16", "1:1"])
    parser.add_argument("--model", default="GEM_PIX_2")
    parser.add_argument("--project-id", default=None)
    parser.add_argument("--proxy", default=None, help="Optional proxy URL for all token tests")
    parser.add_argument("--limit", type=int, default=0, help="Only test first N tokens when > 0")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--remove-profile", action="store_true", help="Delete test Chrome profile after each token")
    parser.add_argument(
        "--account-sessions-url",
        default=DEFAULT_ACCOUNT_SESSIONS_URL,
        help="Admin API URL returning account_name + token mappings",
    )
    parser.add_argument(
        "--api-key",
        default="",
        help="Admin API key for /api/admin/veo/account-sessions",
    )
    parser.add_argument(
        "--map-only",
        action="store_true",
        help="Only call account-sessions API and map tokens; do not create test images",
    )
    args = parser.parse_args()

    tokens_path = Path(args.tokens_file).resolve()
    output_path = Path(args.output).resolve()
    tokens = read_tokens(tokens_path)
    if args.limit > 0:
        tokens = tokens[: args.limit]

    if not tokens:
        raise SystemExit(f"No tokens found in {tokens_path}")

    account_map: dict[str, dict[str, Any]] = {}
    account_map_error = None
    if args.api_key:
        print(f"Fetching account/token map from {args.account_sessions_url}")
        try:
            account_map = fetch_account_token_map(args.account_sessions_url, args.api_key)
            print(f"Loaded {len(account_map)} account token mapping(s).")
        except Exception as exc:
            account_map_error = str(exc)
            print(f"Account map fetch failed: {account_map_error}")

    print(f"Testing {len(tokens)} token(s). Full tokens will not be printed.")
    results = []
    started_at = time.time()
    for index, token in enumerate(tokens, start=1):
        account_info = account_map.get(token) or {}
        if args.map_only:
            status = "MAPPED" if account_info else "NOT_MAPPED"
            print(
                f"[{index:02d}] {mask_token(token)} status={status} "
                f"account={account_info.get('account_name') or '-'}"
            )
            results.append({
                "index": index,
                "token": mask_token(token),
                "status": status,
                "account_name": account_info.get("account_name") or "",
                "account_project_id": account_info.get("project_id") or "",
                "account_proxy": account_info.get("proxy") or "",
            })
            continue
        results.append(test_one_token(index, token, args, account_info))

    counts: dict[str, int] = {}
    for item in results:
        counts[item["status"]] = counts.get(item["status"], 0) + 1

    payload = {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_seconds": round(time.time() - started_at, 2),
        "tokens_file": str(tokens_path),
        "account_sessions_url": args.account_sessions_url if args.api_key else "",
        "account_map_count": len(account_map),
        "account_map_error": account_map_error,
        "counts": counts,
        "results": results,
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Done. counts={counts}")
    print(f"Saved: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
