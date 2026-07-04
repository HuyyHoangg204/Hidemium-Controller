# Tóm tắt thay đổi đã làm

## 1. Pull-based Queue

- Thêm API để Banana Worker tự claim job từ Natha:
  - `POST /api/machines/jobs/claim`
  - `POST /api/machines/jobs/{job_id}/result`
- Job chuyển trạng thái `PENDING` → `PROCESSING` → `COMPLETED/FAILED`.
- Worker xác thực bằng `machine_id` + `machine_secret`.
- Cập nhật curl test trong `machine_api_curl.txt`.

## 2. Public API Orchestration

- Thêm mô hình Natha gọi thẳng public API của Banana Worker.
- Mở rộng register/heartbeat machine với:
  - `api_key_hash`
  - `supported_modes`
  - `max_concurrent_jobs`
  - `public_api_auth_mode`
  - `recent_error_count`
- Thêm endpoint dispatcher thủ công:
  - `POST /api/machines/dispatch/run-once`
  - `POST /api/machines/dispatch/poll-once`
  - `GET /api/machines/dispatch/status`
- Dispatcher chọn machine theo online, slot rảnh, mode hỗ trợ và API key hash.
- Poller lưu kết quả remote task vào `raw_result.public_dispatch`.

## 3. Fix account session

- Sửa lỗi `No active Veo account is assigned to this user`.
- Nếu user chưa có account gán, hệ thống tự fallback sang account active trong pool.
- Cho phép dùng account có `api_session_cached` dù cookie hiện tại rỗng.

## 4. Tài liệu đã thêm

- `public_api_orchestration.md`
- `pull_queue_changes_summary.md`
- Cập nhật `machine_api_curl.txt`

## 5. Verify & Git

- Đã kiểm tra syntax Python thành công bằng `py_compile`.
- Đã push code lên branch `restore-3592172`.
