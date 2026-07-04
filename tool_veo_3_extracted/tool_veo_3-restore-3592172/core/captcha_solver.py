"""
captcha_solver.py

Chỉ dùng PlaywrightCaptchaSolver — mở Chrome headless với profile đã login sẵn
(setup 1 lần qua ⚙️ Chrome Settings trong app.py).

SelfCaptchaSolver và ExtensionCaptchaSolver đã bị loại bỏ hoàn toàn.
"""

import time


def _safe_print(*args, **kwargs):
    """Print that never crashes on non-UTF-8 consoles."""
    try:
        print(*args, **kwargs)
    except (OSError, UnicodeEncodeError):
        try:
            msg = " ".join(str(a) for a in args)
            safe = msg.encode("utf-8", errors="replace").decode("ascii", errors="replace")
            print(safe, **{k: v for k, v in kwargs.items() if k != "end"})
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# Token Cache — dùng chung cho tất cả worker trong cùng process
# ══════════════════════════════════════════════════════════════════════════════

_CAPTCHA_CACHE: dict = {}


def get_cached_captcha(account: str, action: str) -> str | None:
    """Lấy token từ cache nếu còn hạn (TTL 110s)."""
    key = f"{account}_{action}"
    if key in _CAPTCHA_CACHE:
        entry = _CAPTCHA_CACHE[key]
        if time.time() < entry["expires_at"]:
            return entry["token"]
        # Hết hạn → xóa
        del _CAPTCHA_CACHE[key]
    return None


def set_cached_captcha(account: str, action: str, token: str, ttl: int = 110):
    """Lưu token vào cache với TTL (mặc định 110s — token reCAPTCHA có hiệu lực ~120s)."""
    key = f"{account}_{action}"
    _CAPTCHA_CACHE[key] = {"token": token, "expires_at": time.time() + ttl}


def invalidate_cached_captcha(account: str, action: str):
    """Xóa cache của một action (dùng khi nhận 403 reCAPTCHA)."""
    key = f"{account}_{action}"
    if key in _CAPTCHA_CACHE:
        del _CAPTCHA_CACHE[key]
        _safe_print(f"[PW-CAPTCHA] Cache invalidated: {key}")


# ══════════════════════════════════════════════════════════════════════════════
# PlaywrightCaptchaSolver — Mở Chrome headless lấy token (cache 110s)
#
# Cơ chế:
#   - Lần đầu: dùng "Chrome Settings" trong app để đăng nhập thủ công vào
#     chrome_profiles/captcha_worker/ (profile lưu session Google)
#   - Mỗi lần cần token: Playwright launch_persistent_context() với profile đó
#     → Chrome đã login sẵn → vào labs.google không bị redirect
#     → inject grecaptcha.enterprise.execute() → lấy token score ~0.9
#   - Cache 110s → nhiều worker dùng cùng token, tránh mở Chrome liên tục
#   - 403 xảy ra → gọi invalidate_cached_captcha() để force lấy token mới
# ══════════════════════════════════════════════════════════════════════════════

class PlaywrightCaptchaSolver:
    SITE_KEY = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"

    def __init__(
        self,
        account: str,
        cookie_string: str = None,
        proxy: str = None,
        profile_dir: str = None,
    ):
        """
        Args:
            account:        Label định danh (thường là "captcha_worker"), dùng làm cache key.
            cookie_string:  Cookie string (fallback nếu không có profile_dir).
            proxy:          HTTP proxy URL (vd: http://user:pass@host:port).
            profile_dir:    Đường dẫn tuyệt đối đến Chrome user-data-dir đã login sẵn.
                            Ưu tiên hơn cookie_string.
        """
        self.account = account
        self.cookie_string = cookie_string
        self.proxy = proxy
        self.profile_dir = profile_dir
        if self.profile_dir:
            try:
                from core.chrome_cleanup import register_tool_profile
                register_tool_profile(self.profile_dir, owner=self.account)
            except Exception:
                pass

    # ── Parse cookie string thành danh sách Playwright cookie objects ─────────
    def _parse_cookies(self) -> list:
        pw_cookies = []
        if not self.cookie_string:
            return pw_cookies
        for pair in self.cookie_string.split(";"):
            if "=" in pair:
                name, val = pair.strip().split("=", 1)
                cookie = {
                    "name": name,
                    "value": val,
                    "domain": ".google.com",
                    "path": "/",
                }
                if name.startswith("__Secure-"):
                    cookie.update({"httpOnly": True, "secure": True, "sameSite": "None"})
                pw_cookies.append(cookie)
        return pw_cookies

    # ── Core solve logic ────────────────────────────────────────────────────────
    def _solve(self, action: str, row: int = 0) -> str | None:
        # 1. Kiểm tra cache trước
        cached = get_cached_captcha(self.account, action)
        if cached:
            _safe_print(f"[Row {row}] [PW-CAPTCHA] ✅ Used cached token (len={len(cached)})")
            return cached

        _safe_print(
            f"[Row {row}] [PW-CAPTCHA] Opening Chrome headless for action={action} "
            f"(profile={'yes' if self.profile_dir else 'no, using cookie'})"
        )

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            _safe_print(
                "[PW-CAPTCHA] ❌ playwright chưa được cài đặt!\n"
                "Chạy: pip install playwright && playwright install chrome"
            )
            return None

        try:
            with sync_playwright() as p:
                browser_args = [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-infobars",
                    "--disable-extensions",
                    "--window-size=1920,1080",
                ]
                proxy_cfg = {"server": self.proxy} if self.proxy else None

                # Script ẩn fingerprint bot (chạy trước mọi JS trên trang)
                _stealth_js = """
                    Object.defineProperty(navigator, 'webdriver', {get: () => false});
                    Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
                    Object.defineProperty(navigator, 'languages', {get: () => ['vi-VN','vi','en-US','en']});
                    window.chrome = {runtime: {}};
                """
                _ua = (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                )

                browser = None
                if self.profile_dir:
                    # ── Dùng persistent context (profile đã login sẵn) ───────
                    _safe_print(f"[Row {row}] [PW-CAPTCHA] Profile dir: {self.profile_dir}")
                    context = p.chromium.launch_persistent_context(
                        user_data_dir=self.profile_dir,
                        headless=False,          # NON-headless → score cao hơn
                        channel="chrome",
                        args=browser_args + [
                            "--window-position=-32000,-32000",  # Đẩy ra ngoài màn hình
                        ],
                        proxy=proxy_cfg,
                        bypass_csp=True,
                        user_agent=_ua,
                        viewport={"width": 1920, "height": 1080},
                    )
                    context.add_init_script(_stealth_js)
                    page = context.new_page()
                else:
                    # ── Fallback: dùng cookie string ─────────────────────────
                    _safe_print(f"[Row {row}] [PW-CAPTCHA] Fallback mode (cookie string)")
                    browser = p.chromium.launch(
                        headless=True,
                        channel="chrome",
                        args=browser_args,
                        proxy=proxy_cfg,
                    )
                    context = browser.new_context(
                        bypass_csp=True,
                        user_agent=_ua,
                        viewport={"width": 1920, "height": 1080},
                    )
                    context.add_init_script(_stealth_js)
                    context.add_cookies(self._parse_cookies())
                    page = context.new_page()

                try:
                    # Navigate — chờ DOM load để recaptcha script có thời gian inject
                    page.goto(
                        "https://labs.google/fx/tools/flow",
                        wait_until="domcontentloaded",
                        timeout=60000,
                    )

                    # Chờ grecaptcha.enterprise.execute là function thực sự (không chỉ defined)
                    try:
                        page.wait_for_function(
                            "typeof grecaptcha !== 'undefined' && "
                            "typeof grecaptcha.enterprise !== 'undefined' && "
                            "typeof grecaptcha.enterprise.execute === 'function'",
                            timeout=20000,
                        )
                    except Exception:
                        _safe_print(f"[Row {row}] [PW-CAPTCHA] Timeout waiting for grecaptcha.enterprise.execute")
                        return None

                    # Simulate mouse interaction → tăng score reCAPTCHA (bot detection)
                    import random as _rnd
                    try:
                        page.mouse.move(_rnd.randint(200, 800), _rnd.randint(200, 500))
                        page.mouse.move(_rnd.randint(300, 700), _rnd.randint(300, 600), steps=5)
                        page.mouse.wheel(0, _rnd.randint(100, 300))
                        time.sleep(0.3)
                        page.mouse.wheel(0, -_rnd.randint(50, 150))
                    except Exception:
                        pass

                    # Retry execute tối đa 3 lần trong cùng session (tránh mở Chrome lại)
                    token = None
                    for _attempt in range(3):
                        if _attempt > 0:
                            _safe_print(f"[Row {row}] [PW-CAPTCHA] Retry inject #{_attempt} (chờ 2s)...")
                            time.sleep(2)

                        try:
                            token = page.evaluate(f"""async () => {{
                                try {{
                                    return await grecaptcha.enterprise.execute(
                                        "{self.SITE_KEY}", {{action: "{action}"}});
                                }} catch(e) {{
                                    return null;
                                }}
                            }}""")
                        except Exception as _ee:
                            _safe_print(f"[Row {row}] [PW-CAPTCHA] evaluate error: {_ee}")
                            token = None

                        if token and isinstance(token, str) and len(token) > 20:
                            break  # thành công → thoát vòng retry
                        token = None

                    if token:
                        set_cached_captcha(self.account, action, token, ttl=110)
                        _safe_print(
                            f"[Row {row}] [PW-CAPTCHA] ✅ Token OK (len={len(token)})"
                        )
                    else:
                        _safe_print(f"[Row {row}] [PW-CAPTCHA] ❌ No token after 3 attempts")

                    return token

                finally:
                    try:
                        context.close()
                    except Exception:
                        pass
                    try:
                        if browser:
                            browser.close()
                    except Exception:
                        pass
                    if self.profile_dir:
                        try:
                            from core.chrome_cleanup import cleanup_owner
                            cleanup_owner(self.account, reason="captcha_session_done")
                        except Exception:
                            pass

        except Exception as e:
            _safe_print(f"[Row {row}] [PW-CAPTCHA] ❌ Exception: {e}")
            return None

    # ── Public API ─────────────────────────────────────────────────────────────
    def solve_imagen(self, row: int = 0) -> str | None:
        """Lấy token cho Imagen — IMAGE_GENERATION action."""
        return self._solve("IMAGE_GENERATION", row)

    def solve_veo3(self, row: int = 0) -> str | None:
        """Lấy token cho action VIDEO_GENERATION (dùng cho Veo)."""
        return self._solve("VIDEO_GENERATION", row)

    def browser_post_json(self, action: str, url: str, request_headers: dict,
                          payload_fn, row: int = 0):
        """
        Gộp captcha + API call trong cùng 1 Chrome session.
        payload_fn(token) -> dict  — callback nhận token và trả về payload dict.
        Trả về (token, response_dict) hoặc (None, None) nếu lỗi.

        Fetch từ Chrome → real TLS fingerprint, origin labs.google
        → pass mọi reCAPTCHA / bot-detection validation của Google.
        """
        import json as _json

        cached_token = get_cached_captcha(self.account, action)

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            _safe_print("[PW-CAPTCHA] playwright chưa cài.")
            return None, None

        try:
            with sync_playwright() as p:
                _args = [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-infobars",
                    "--window-size=1920,1080",
                    "--window-position=-32000,-32000",
                ]
                _stealth = (
                    "Object.defineProperty(navigator,'webdriver',{get:()=>false});"
                    "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
                    "Object.defineProperty(navigator,'languages',{get:()=>['vi-VN','vi','en-US','en']});"
                    "window.chrome={runtime:{}};"
                )
                _ua = (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                )
                proxy_cfg = {"server": self.proxy} if self.proxy else None

                if self.profile_dir:
                    try:
                        from core.chrome_cleanup import register_tool_profile
                        register_tool_profile(self.profile_dir, owner=self.account)
                    except Exception:
                        pass
                context = p.chromium.launch_persistent_context(
                    user_data_dir=self.profile_dir,
                    headless=False,
                    channel="chrome",
                    args=_args,
                    proxy=proxy_cfg,
                    bypass_csp=True,
                    user_agent=_ua,
                    viewport={"width": 1920, "height": 1080},
                )
                context.add_init_script(_stealth)
                page = context.new_page()

                try:
                    page.goto(
                        "https://labs.google/fx/tools/flow",
                        wait_until="domcontentloaded",
                        timeout=60000,
                    )

                    token = cached_token
                    if not token:
                        try:
                            page.wait_for_function(
                                "typeof grecaptcha!=='undefined'&&"
                                "typeof grecaptcha.enterprise!=='undefined'&&"
                                "typeof grecaptcha.enterprise.execute==='function'",
                                timeout=20000,
                            )
                        except Exception:
                            _safe_print(f"[Row {row}] [PW-POST] Timeout grecaptcha")
                            return None, None

                        import random as _rnd
                        try:
                            page.mouse.move(_rnd.randint(200, 700), _rnd.randint(200, 500))
                            page.mouse.move(_rnd.randint(300, 600), _rnd.randint(300, 600), steps=5)
                            page.mouse.wheel(0, _rnd.randint(100, 300))
                            time.sleep(0.3)
                        except Exception:
                            pass

                        token = page.evaluate(
                            f"""async () => {{
                                try {{
                                    return await grecaptcha.enterprise.execute(
                                        "{self.SITE_KEY}", {{action: "{action}"}});
                                }} catch(e) {{ return null; }}
                            }}"""
                        )

                        if token and isinstance(token, str) and len(token) > 20:
                            set_cached_captcha(self.account, action, token, ttl=110)
                            _safe_print(f"[Row {row}] [PW-POST] ✅ Token OK (len={len(token)})")
                        else:
                            _safe_print(f"[Row {row}] [PW-POST] ❌ No token")
                            return None, None

                    # Xây payload với token qua callback
                    payload = payload_fn(token)
                    payload_str = _json.dumps(payload, separators=(",", ":"))
                    hdrs = {k: v for k, v in request_headers.items() if v}
                    _safe_print(f"[Row {row}] [PW-POST] Sending via context.request → {url[:70]}...")

                    # context.request.fetch() = chạy ngoài JS sandbox → không có CORS
                    # Tự động dùng Chrome cookies từ persistent profile để auth
                    try:
                        api_resp = context.request.fetch(
                            url,
                            method="POST",
                            headers=hdrs,
                            data=payload_str,
                            timeout=120000,
                            ignore_https_errors=True,
                        )
                        status = api_resp.status
                        body = api_resp.text()
                        _safe_print(f"[Row {row}] [PW-POST] HTTP {status} | preview={body[:120]}")

                        if status == 200:
                            try:
                                return token, _json.loads(body)
                            except Exception:
                                return token, None

                        _safe_print(f"[Row {row}] [PW-POST] Non-200: {body[:300]}")
                        return token, None

                    except Exception as _fe:
                        _safe_print(f"[Row {row}] [PW-POST] Request error: {_fe}")
                        return token, None


                finally:
                    try:
                        context.close()
                    except Exception:
                        pass
                    if self.profile_dir:
                        try:
                            from core.chrome_cleanup import cleanup_owner
                            cleanup_owner(self.account, reason="browser_post_done")
                        except Exception:
                            pass

        except Exception as e:
            _safe_print(f"[Row {row}] [PW-POST] Exception: {e}")
            return None, None
