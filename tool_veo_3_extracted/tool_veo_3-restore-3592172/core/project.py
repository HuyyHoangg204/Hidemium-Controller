import json
import time
import socket
import concurrent.futures
from typing import Optional, Dict, Any

from core.http_helpers import request_with_retries, HTTPError
from core.cookie_manager import extract_session_token_cookie


def _safe_print(*args, **kwargs):
    try:
        print(*args, **kwargs)
    except (OSError, UnicodeEncodeError):
        try:
            msg = " ".join(str(a) for a in args)
            safe = msg.encode("utf-8", errors="replace").decode("ascii", errors="replace")
            print(safe, **{k: v for k, v in kwargs.items() if k != "end"})
        except Exception:
            pass


TRPC_BASE = "https://labs.google/fx/api/trpc"
# Cached IP resolve để bypass system DNS
_LABS_GOOGLE_IP = None

# DoH servers để resolve (fallback lần lượt)
_DOH_SERVERS = [
    "https://8.8.8.8/resolve",
    "https://1.1.1.1/dns-query",
    "https://dns.google/resolve",
]


def _resolve_via_doh(hostname: str = "labs.google", timeout: int = 5) -> Optional[str]:
    """Resolve hostname qua DNS-over-HTTPS (bỏ qua system DNS hoàn toàn).
    Lực tuần tự qua các DoH servers cho đến khi resolve thành công.
    Trả về IP string hoặc None nếu tất cả đều fail.
    """
    import urllib.request
    import urllib.parse

    for doh_url in _DOH_SERVERS:
        try:
            params = urllib.parse.urlencode({"name": hostname, "type": "A"})
            req = urllib.request.Request(
                f"{doh_url}?{params}",
                headers={"Accept": "application/dns-json"},
            )
            # urllib.request.urlopen có timeout thực sự
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            answers = data.get("Answer", []) or data.get("answer", [])
            for ans in answers:
                if ans.get("type") == 1:  # A record
                    ip = ans.get("data", "").strip()
                    if ip:
                        _safe_print(f"[DNS] Resolved {hostname} -> {ip} via DoH ({doh_url})")
                        return ip
        except Exception as e:
            _safe_print(f"[DNS] DoH {doh_url} failed: {e}")
            continue
    return None


def _get_labs_google_ip(timeout: int = 5) -> Optional[str]:
    """Lấy IP của labs.google: ưu tiên cache -> DoH -> system DNS (fallback)."""
    global _LABS_GOOGLE_IP
    # Dùng cache nếu đã có
    if _LABS_GOOGLE_IP:
        return _LABS_GOOGLE_IP

    # Thử DoH trước (bỏ qua system DNS)
    ip = _resolve_via_doh("labs.google", timeout=timeout)
    if ip:
        _LABS_GOOGLE_IP = ip
        return ip

    # Fallback: system DNS với thread timeout
    def _system_dns():
        return socket.getaddrinfo("labs.google", 443, socket.AF_INET)[0][4][0]
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            ip = ex.submit(_system_dns).result(timeout=timeout)
        if ip:
            _LABS_GOOGLE_IP = ip
            return ip
    except Exception:
        pass

    return None


def _build_auth_headers(
    cookie: Optional[str] = None,
    access_token: Optional[str] = None,
    browser_headers: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    # Start with browser headers if provided (Origin, Referer, UA, sec-fetch-*, x-browser-*)
    headers = dict(browser_headers) if browser_headers else {}
    headers["Content-Type"] = "application/json"
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    if cookie:
        cookie_val = extract_session_token_cookie(cookie)
        if cookie_val:
            headers["Cookie"] = cookie_val
    return headers


def _verify_project_exists(
    project_id: str,
    cookie: Optional[str] = None,
    access_token: Optional[str] = None,
    timeout: int = 10,
) -> bool:
    """Verify project exists by calling TRPC `project.getProject` or `project.searchProjectScenes` with GET and checking for the id in the response.

    The frontend uses GET with an `input` query parameter (see HAR), so replicate that format.
    """
    import json as _json

    headers = _build_auth_headers(cookie=cookie, access_token=access_token)

    # Try GET project.getProject first (matches observed browser calls)
    url = f"{TRPC_BASE}/project.getProject"
    params = {
        "input": _json.dumps({"json": {"projectId": project_id, "toolName": "PINHOLE"}})
    }
    try:
        resp = request_with_retries(
            "GET", url, headers=headers, params=params, timeout=timeout
        )
        if resp and getattr(resp, "status_code", None) == 200:
            try:
                jr = resp.json()
            except Exception:
                jr = None
            if jr:

                def find_pid(obj):
                    if isinstance(obj, dict):
                        for v in obj.values():
                            if find_pid(v):
                                return True
                    elif isinstance(obj, list):
                        for v in obj:
                            if find_pid(v):
                                return True
                    elif isinstance(obj, str):
                        if obj == project_id:
                            return True
                    return False

                if find_pid(jr):
                    return True

        # Fallback: try searchProjectScenes which will return scenes for a valid project
        url2 = f"{TRPC_BASE}/project.searchProjectScenes"
        params2 = {
            "input": _json.dumps(
                {
                    "json": {
                        "projectId": project_id,
                        "toolName": "PINHOLE",
                        "pageSize": 1,
                    }
                }
            )
        }
        resp2 = request_with_retries(
            "GET", url2, headers=headers, params=params2, timeout=timeout
        )
        if resp2 and getattr(resp2, "status_code", None) == 200:
            try:
                jr2 = resp2.json()
            except Exception:
                jr2 = None
            if jr2:

                def find_pid2(obj):
                    if isinstance(obj, dict):
                        for v in obj.values():
                            if find_pid2(v):
                                return True
                    elif isinstance(obj, list):
                        for v in obj:
                            if find_pid2(v):
                                return True
                    elif isinstance(obj, str):
                        if obj == project_id:
                            return True
                    return False

                return bool(find_pid2(jr2))
    except Exception:
        return False
    return False


def _cffi_request(
    method: str, url: str, headers: dict, body: Any = None, timeout: int = 8
):
    """HTTP request using standard requests library."""
    import json as _json
    import requests as _req

    data = _json.dumps(body) if body is not None else None
    resp = _req.request(method, url, headers=headers, data=data, timeout=timeout, verify=False)
    return resp


def _find_project_id(jr):
    """Tim projectId de quy trong JSON response."""
    if not isinstance(jr, dict):
        return None
    result = jr.get("result", {})
    if isinstance(result, dict):
        for key in ("data", "value"):
            d = result.get(key)
            if isinstance(d, dict) and "projectId" in d:
                return d["projectId"]
        if "projectId" in result:
            return result["projectId"]

    def _find(obj):
        if isinstance(obj, dict):
            if (
                "projectId" in obj
                and isinstance(obj["projectId"], str)
                and "-" in obj["projectId"]
            ):
                return obj["projectId"]
            for v in obj.values():
                r = _find(v)
                if r:
                    return r
        elif isinstance(obj, list):
            for v in obj:
                r = _find(v)
                if r:
                    return r
        return None

    return _find(jr)


def create_project(
    project_title: str = "AutoVoice Project",
    tool_name: str = "PINHOLE",
    cookie: Optional[str] = None,
    access_token: Optional[str] = None,
    browser_headers: Optional[Dict[str, str]] = None,
    timeout: int = 8,
    proxy: Optional[str] = None,
) -> Optional[str]:
    """Create a project via TRPC. Thu 3 auth variants theo thu tu uu tien.
    
    Args:
        proxy: HTTP/SOCKS5 proxy string, ví dụ: 'http://127.0.0.1:7890'
               Khi có proxy, cả DoH resolve lẫn HTTP request đều đi qua proxy.
    """
    # Resolve IP: nếu có proxy thì DoH đi qua proxy luôn
    from core.dns_helper import get_curl_resolve as _get_resolve
    curl_resolve = _get_resolve("labs.google", proxy=proxy)
    if not curl_resolve and not proxy:
        # Không resolve được qua DoH lẫn system DNS
        _safe_print("[Project] Cannot resolve labs.google via DoH or system DNS, skipping")
        return None
    # curl_cffi resolve option: inject IP trực tiếp, bỏ qua DNS hoàn toàn
    # Nếu có proxy: proxy tự xử lý DNS, không cần resolve
    url = f"{TRPC_BASE}/project.createProject"
    body_str = json.dumps(
        {"json": {"projectTitle": project_title, "toolName": tool_name}}
    )

    # Base browser headers (UA, Origin, Referer, x-browser-*)
    base = {
        k: v
        for k, v in (browser_headers or {}).items()
        if k.lower() not in ("cookie", "authorization", "content-type")
    }
    base["Content-Type"] = "application/json"

    # Cookie string - uu tien full cookie tu client (co nhieu cookie hon)
    cookie_str = None
    if browser_headers and browser_headers.get("Cookie"):
        cookie_str = browser_headers["Cookie"]
    elif cookie:
        from core.cookie_manager import extract_session_token_cookie

        cookie_str = extract_session_token_cookie(cookie) or cookie

    # 3 variants: Cookie-only -> Bearer+Cookie -> Bearer-only
    variants = []
    if cookie_str:
        variants.append({**base, "Cookie": cookie_str})
    if access_token and cookie_str:
        variants.append(
            {**base, "Authorization": f"Bearer {access_token}", "Cookie": cookie_str}
        )
    if access_token:
        variants.append({**base, "Authorization": f"Bearer {access_token}"})

    if not variants:
        _safe_print("[Project] No auth credentials available")
        return None

    for i, hdrs in enumerate(variants):
        hdrs = {k: v for k, v in hdrs.items() if v}
        try:
            import requests as _req
            proxies = {"http": proxy, "https": proxy} if proxy else None
            resp = _req.post(url, headers=hdrs, data=body_str, timeout=timeout, proxies=proxies, verify=False)
            status = getattr(resp, "status_code", None)
            _safe_print(f"[Project] variant={i+1}/{len(variants)} status={status}")
            if status == 200:
                try:
                    jr = resp.json()
                    _safe_print(f"[Project] resp={json.dumps(jr)[:300]}")
                except Exception:
                    _safe_print(f"[Project] raw={resp.text[:200]}")
                    continue
                pid = _find_project_id(jr)
                if pid:
                    _safe_print(f"[Project] project_id={pid!r}")
                    return pid
                _safe_print("[Project] 200 but no projectId found")
            else:
                _safe_print(f"[Project] variant={i+1} status={status}, trying next...")
        except Exception as e:
            _safe_print(f"[Project] variant={i+1} error: {e}")

    _safe_print("[Project] All variants failed")
    return None


def search_scenes(
    input_text: str,
    cookie: Optional[str] = None,
    access_token: Optional[str] = None,
    timeout: int = 10,
) -> Optional[Dict[str, Any]]:
    """Call TRPC `project.searchProjectScenes?input=...` and return parsed JSON or None."""
    url = f"{TRPC_BASE}/project.searchProjectScenes"
    headers = _build_auth_headers(cookie=cookie, access_token=access_token)
    params = {"input": input_text}
    try:
        resp = request_with_retries(
            "GET", url, headers=headers, params=params, timeout=timeout
        )
        return resp.json()
    except HTTPError:
        return None
    except Exception:
        return None


def search_user_projects(
    cookie: Optional[str] = None,
    access_token: Optional[str] = None,
    page_size: int = 20,
    timeout: int = 10,
    proxy: Optional[str] = None,
) -> Optional[list]:
    """Call TRPC `project.searchUserProjects` and return list of {'projectId','projectTitle'} or None."""
    import json as _json

    url = f"{TRPC_BASE}/project.searchUserProjects"
    headers = _build_auth_headers(cookie=cookie, access_token=access_token)
    params = {
        "input": _json.dumps(
            {
                "json": {"pageSize": page_size, "toolName": "PINHOLE", "cursor": None},
                "meta": {"values": {"cursor": ["undefined"]}},
            }
        )
    }
    try:
        # Dùng requests tiêu chuẩn
        import requests as _req
        proxies = {"http": proxy, "https": proxy} if proxy else None
        resp = _req.get(url, headers=headers, params=params, timeout=timeout, proxies=proxies, verify=False)

        if not resp or getattr(resp, "status_code", None) != 200:
            return None
        jr = resp.json()
        results = []

        # Traverse response to find any object carrying projectId. The TRPC
        # response can be result.data.json=[{projectId, projectInfo, ...}], so
        # do not require parent keys like "items" or "projects".
        seen = set()

        def find_projects(obj):
            if isinstance(obj, dict):
                pid = obj.get("projectId") or obj.get("id")
                if isinstance(pid, str) and pid.strip() and pid not in seen:
                    info = obj.get("projectInfo") if isinstance(obj.get("projectInfo"), dict) else {}
                    title = (
                        info.get("projectTitle")
                        or obj.get("projectTitle")
                        or obj.get("title")
                    )
                    seen.add(pid)
                    results.append({"projectId": pid.strip(), "projectTitle": title})
                for v in obj.values():
                    find_projects(v)
            elif isinstance(obj, list):
                for v in obj:
                    find_projects(v)

        find_projects(jr)
        _safe_print(f"[Project] searchUserProjects status=200 count={len(results)}")
        return results
    except Exception:
        return None
