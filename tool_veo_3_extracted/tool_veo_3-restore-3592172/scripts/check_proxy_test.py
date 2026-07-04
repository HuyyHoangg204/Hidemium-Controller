"""Quick proxy checker.

Usage:
  python scripts/check_proxy_test.py
  python scripts/check_proxy_test.py --file proxy_pool.txt --output live_proxies.txt

The script accepts proxies in these formats:
  host:port
  host:port:user:pass
  user:pass@host:port
  http://user:pass@host:port

By default it reads proxies from ``proxy_pool.txt`` and writes usable proxies
to ``live_proxies.txt``.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROXY_FILE = PROJECT_ROOT / "proxy_pool.txt"
DEFAULT_OUTPUT_FILE = PROJECT_ROOT / "live_proxies.txt"

DEFAULT_PROXIES = [
    "171.241.50.103:14015",
    "42.116.143.210:14989",
    "118.69.141.114:15251",
    "118.68.128.124:34646",
    "171.224.236.39:20859",
]

TEST_URLS = [
    "http://httpbin.org/ip",
    "https://api.ipify.org?format=json",
]


def normalize_proxy(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""

    # Provider format: HTTP|host|port|user|password
    # Example: HTTP|ntviet.mikproxy.online|10458|USQ5RY|ntviet
    if "|" in value:
        parts = [part.strip() for part in value.split("|")]
        if len(parts) == 5 and all(parts):
            scheme, host, port, user, password = parts
            scheme = scheme.lower()
            if scheme in {"http", "https", "socks4", "socks5"}:
                return f"{scheme}://{user}:{password}@{host}:{port}"
        raise ValueError(f"Unsupported pipe proxy format: {raw}")

    if "://" in value:
        return value
    parts = value.split(":")
    if len(parts) == 4 and all(parts):
        host, port, user, password = parts
        return f"http://{user}:{password}@{host}:{port}"
    return f"http://{value}"


def format_live_proxy(raw: str) -> str:
    """Return proxy in host:port:user:password format for proxy.txt compatibility."""
    value = (raw or "").strip()
    if not value:
        return ""

    if "|" in value:
        parts = [part.strip() for part in value.split("|")]
        if len(parts) == 5 and all(parts):
            _scheme, host, port, user, password = parts
            return f"{host}:{port}:{user}:{password}"
        raise ValueError(f"Unsupported pipe proxy format: {raw}")

    if "://" in value:
        without_scheme = value.split("://", 1)[1]
        if "@" in without_scheme:
            auth, host_port = without_scheme.rsplit("@", 1)
            if ":" in auth and ":" in host_port:
                user, password = auth.split(":", 1)
                host, port = host_port.rsplit(":", 1)
                return f"{host}:{port}:{user}:{password}"
        return without_scheme

    parts = value.split(":")
    if len(parts) == 4 and all(parts):
        return value
    return value


def resolve_path(path: str | None, default_path: Path) -> Path:
    if not path:
        return default_path
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    if candidate.exists():
        return candidate
    return PROJECT_ROOT / candidate


def load_proxies(path: str | None) -> list[str]:
    proxy_path = resolve_path(path, DEFAULT_PROXY_FILE)
    if proxy_path.exists():
        return [
            line.strip()
            for line in proxy_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    if path:
        raise FileNotFoundError(f"Proxy file not found: {proxy_path}")
    return DEFAULT_PROXIES


def save_live_proxies(path: str | None, live_proxies: list[str]) -> Path:
    output_path = resolve_path(path, DEFAULT_OUTPUT_FILE)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(live_proxies) + ("\n" if live_proxies else ""), encoding="utf-8")
    return output_path


def check_proxy(raw_proxy: str, timeout: float) -> dict:
    normalized = normalize_proxy(raw_proxy)
    started = time.perf_counter()
    last_error = ""
    for url in TEST_URLS:
        try:
            opener = build_opener(ProxyHandler({"http": normalized, "https": normalized}))
            request = Request(url, headers={"User-Agent": "proxy-check/1.0"})
            with opener.open(request, timeout=timeout) as response:
                body = response.read(300).decode("utf-8", errors="replace").strip()
                elapsed = time.perf_counter() - started
                return {
                    "ok": True,
                    "proxy": raw_proxy,
                    "normalized": normalized,
                    "url": url,
                    "status": getattr(response, "status", "OK"),
                    "elapsed": elapsed,
                    "body": body,
                    "error": "",
                }
        except Exception as exc:  # noqa: BLE001 - diagnostic script
            last_error = f"{type(exc).__name__}: {exc}"
    elapsed = time.perf_counter() - started
    return {
        "ok": False,
        "proxy": raw_proxy,
        "normalized": normalized,
        "url": "",
        "status": "",
        "elapsed": elapsed,
        "body": "",
        "error": last_error or str(URLError("unknown proxy error")),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Check whether proxies are usable")
    parser.add_argument("--file", default=None, help=f"Proxy file, one proxy per line (default: {DEFAULT_PROXY_FILE})")
    parser.add_argument("--output", default=None, help=f"Output file for usable proxies (default: {DEFAULT_OUTPUT_FILE})")
    parser.add_argument("--timeout", type=float, default=8.0, help="Timeout per request in seconds")
    parser.add_argument("--workers", type=int, default=8, help="Concurrent checks")
    args = parser.parse_args()

    proxies = load_proxies(args.file)
    if not proxies:
        raise SystemExit("No proxies to check")

    input_path = resolve_path(args.file, DEFAULT_PROXY_FILE)
    print(f"Checking {len(proxies)} proxy/proxies from {input_path}...")
    ok_count = 0
    live_proxies: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = [executor.submit(check_proxy, proxy, args.timeout) for proxy in proxies]
        for index, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            result = future.result()
            if result["ok"]:
                ok_count += 1
                live_proxies.append(format_live_proxy(result["proxy"]))
                print(
                    f"[OK] {result['proxy']} | {result['elapsed']:.2f}s | "
                    f"via={result['url']} | body={result['body']}"
                )
            else:
                print(f"[FAIL] {result['proxy']} | {result['elapsed']:.2f}s | {result['error']}")

    output_path = save_live_proxies(args.output, live_proxies)
    print(
        f"Done. usable={ok_count}/{len(proxies)} failed={len(proxies) - ok_count} "
        f"output={output_path}"
    )


if __name__ == "__main__":
    main()
