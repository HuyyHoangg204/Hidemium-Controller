# Project: tool_veo_3

## Rule quan trọng về account/proxy

### 1. `_resolve_client_for_task` Section 1.5 — locked account retry proxy VÔ HẠN, KHÔNG fall through Pool

Khi `task.picked_account_name` đã có (Smart Queue đã lock account cho task này), nếu init client thất bại do **proxy chết** (DNS fail / 402 / connection refused), code phải **retry vô hạn với nhiều proxy variants trên CÙNG cookie locked** chứ KHÔNG được fall through xuống Section 2 Pool.

**Implementation** (web/veo_service.py ~line 2841): vòng `while True` cycle qua 3 proxy variants (default → KiotProxy rotate → static fresh) → tất cả fail thì sleep 5s → cycle lại. Chỉ thoát khi (a) init OK hoặc (b) `_check_task_timeout(task)` trigger.

**Timeout guard duy nhất** (line ~538):
- `IMAGE_TASK_TIMEOUT = 600s` (10 phút) cho ảnh
- `VIDEO_TASK_TIMEOUT = 1200s` (20 phút) cho video

Vượt timeout → `_check_task_timeout` tự set FAILED + release cookie + return True → Section 1.5 thoát, return None.

### 2. Upload ảnh tham chiếu fail → retry VÔ HẠN, KHÔNG FAIL, KHÔNG swap

Áp dụng cho cả 3 flow upload:
- **I2V-B64** upload_failed block (~line 4606): bỏ blacklist+swap khi 401 vượt threshold, bỏ swap khi status ≠ 401. Khi upload fail → reload cookie từ DB + xoay proxy (KiotProxy rotate → static random) + continue. `I2V_MAX_ATTEMPTS = 999999` (effective unlimited, chỉ timeout 1200s cancel).
- **I2V cũ** upload retry loop (~line 4023): chuyển từ `for range(_MAX_UPLOAD_RETRIES)` sang `while True` với timeout guard. Bỏ swap khi 401 vượt threshold/auth fail. Khi fail → reload cookie + xoay proxy + retry SAME account.
- **CreateImage** upload section (~line 5378): bỏ FAIL khi pool exhausted/non-proxy err. Wrap thành `while True` cycle: tất cả ref upload OK → break; có ref fail → reload cookie + xoay proxy + retry SAME account. Chỉ timeout 600s mới cancel.

### 3. CreateImage gặp 401 → retry VÔ HẠN, KHÔNG FAIL, KHÔNG swap

Trong `_run_create_image`, khi API trả 401 / "auth variants failed":
- KHÔNG được FAIL task (kể cả khi nghi cookie hết hạn).
- KHÔNG được swap account.
- Phải **xoay proxy từ pool** (KiotProxy rotate → static pool fallback) + **reload cookie từ DB** → retry trên CÙNG account, lặp đến khi success hoặc timeout.

**Implementation** (web/veo_service.py ~line 5738): reload cookie + rotate KiotProxy → fallback `get_random_proxy()` → `continue` với `_should_increment = False` (không tăng retry counter). Vòng `while api_retry < MAX_API_RETRIES` đã có timeout check ở đầu mỗi iteration nên 401 cycle vô hạn cho tới khi timeout 600s/1200s cancel.

### 3. Các flow khác (T2V / I2V / I2V-B64 / CreateImage-other) — chưa apply rule no-swap

Hiện tại các flow này vẫn có logic swap account khi gặp lỗi (failed_accounts.add + picked_account_name = None). Nếu user yêu cầu sửa, cần **xác nhận trước** — đừng tự ý mở rộng rule no-swap toàn project.

## Cấu trúc project (tóm tắt)

- `web/veo_service.py`: orchestrator chính cho video/image tasks (T2V, I2V, I2V-B64, CreateImage). 6000+ LOC.
- `core/veo_client.py`: HTTP client gọi Google Labs API.
- `core/static_proxy_pool.py`: pool proxy tĩnh dùng chung khi KiotProxy cooldown. Hàm `get_fresh_proxy()` / `get_random_proxy()`.
- `core/captcha_pool.py`: pool reCAPTCHA token (Extension + Remote).
- `logs/veo_api.log`: log chính, cực dài (~28K dòng/run).
