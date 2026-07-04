# Tóm tắt thay đổi Pull-based Machine Job Queue

Đã triển khai cơ chế **Worker Pull Job** để Banana Worker tự lấy job từ Natha Registry thay vì backend push xuống worker.

## Thay đổi chính

- Thêm API claim job:
  - `POST /api/machines/jobs/claim`
  - Worker gửi `machine_id`, `machine_secret`, `available_slots`, `supported_modes` để nhận job.
- Thêm API trả kết quả:
  - `POST /api/machines/jobs/{job_id}/result`
  - Worker báo `completed` hoặc `failed`, kèm `download_url`, `duration_seconds`, `error`.
- Xác thực worker bằng `machine_id` + `machine_secret`.
- Job được claim sẽ đổi trạng thái:
  - `PENDING` → `PROCESSING`
- Job report xong sẽ đổi trạng thái:
  - `completed` → `COMPLETED`
  - `failed` → `FAILED`
- Claim job theo số slot rảnh `available_slots` và loại worker hỗ trợ `supported_modes`.
- Lưu metadata worker vào `raw_result.worker_claim` và `raw_result.worker_result`.

## File đã sửa

- `web/store.py`: thêm logic atomic claim job và save result.
- `web/server.py`: thêm API claim/result cho backend Flask.
- `server.py`: thêm bản local simple để test queue bằng file.
- `machine_api_curl.txt`: thêm curl mẫu claim job và report result.

## Verify & Git

Đã kiểm tra syntax thành công:

```powershell
python -m py_compile server.py web/server.py web/store.py
```

Đã push lên branch `restore-3592172`:

```text
commit af80412 - Add pull based machine job queue
```
