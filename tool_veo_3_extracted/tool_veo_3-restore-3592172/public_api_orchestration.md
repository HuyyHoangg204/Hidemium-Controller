# Natha x Banana Public API Orchestration Spec

Contract triển khai mô hình Banana Worker expose public API, Natha Service làm dispatcher chính.

## Luồng chính

```text
Banana Worker mở local API
-> public local API bằng FRP/Caddy/Cloudflare
-> register + heartbeat về Natha Service
-> Natha Service chọn machine phù hợp
-> Natha Service gọi thẳng public API của Banana Worker
-> Natha Service poll task status và lưu kết quả
```

Khác với pull-based queue, Banana Worker không claim job. Natha Service là dispatcher chính.

## Phase A đã triển khai trong repo

- Registry nhận thêm field orchestration:
  - `api_key_hash`
  - `supported_modes`
  - `max_concurrent_jobs`
  - `public_api_auth_mode`
  - `public_api_auth_token`
  - `machine_api_key`
  - `recent_error_count`
  - `cooldown_until`
  - `avg_duration_seconds`
  - `last_error`
- Dispatcher manual endpoints:
  - `POST /api/machines/dispatch/run-once`
  - `POST /api/machines/dispatch/poll-once`
  - `GET /api/machines/dispatch/status`
- Dispatcher chọn machine theo:
  - online heartbeat
  - accepting jobs
  - supported mode image/video
  - effective available slots
  - ưu tiên `api_key_hash` match nếu task metadata có hash
- Natha gọi public Banana API với:
  - `Idempotency-Key: <natha_job_id>`
  - `X-Machine-Secret: <machine_secret>` nếu có
  - fallback `Authorization: Bearer <public_api_auth_token>` hoặc `X-API-Key`
- Poller gọi:
  - `GET {public_url}/api/veo/tasks/{remote_task_id}`
- Kết quả lưu vào `VideoTask.raw_result.public_dispatch`.

## Public Banana create endpoints

| Action type | Endpoint |
|---|---|
| `CREATE_IMAGE` | `/api/veo/images` |
| `IMAGES_TO_IMAGE` | `/api/veo/images/from-images` |
| `TEXT_TO_VIDEO` | `/api/veo/videos/from-text` |
| `IMAGE_TO_VIDEO` | `/api/veo/videos/from-start-image` |
| `FRAMES_TO_VIDEO` | `/api/veo/videos/from-start-end-images` |
| `REFERENCE_IMAGE_TO_VIDEO` | `/api/veo/videos/from-reference-image` |

## Registry sample

```json
{
  "machine_id": "banana-1b70b5f32555",
  "machine_secret": "machine-secret",
  "public_url": "https://banana-1b70b5f32555.nathabanana.xyz",
  "version": "0.1.8",
  "api_key_hash": "sha256-api-key",
  "supported_modes": ["image", "video"],
  "public_api_auth_mode": "machine_secret",
  "max_concurrent_jobs": 25
}
```

## Heartbeat sample

```json
{
  "machine_id": "banana-1b70b5f32555",
  "public_url": "https://banana-1b70b5f32555.nathabanana.xyz",
  "status": "online",
  "frp_status": "running",
  "supported_modes": ["image", "video"],
  "token_count": 5,
  "max_concurrent_jobs": 25,
  "running_jobs": 0,
  "available_slots": 25,
  "accepting_jobs": true,
  "api_key_hash": "sha256-api-key"
}
```

## Ghi chú

- Pull-based queue APIs cũ vẫn được giữ để tương thích.
- Phase A chưa bật scheduler loop tự động; dùng endpoint `run-once` và `poll-once` để kiểm soát an toàn.
- Các policy nâng cao như fair-share, reserved/borrowable slot, cooldown tự động sẽ làm ở phase sau.
