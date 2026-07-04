# NathaMedia API Docs

Base URL: `https://nathamedia.net`

Auth header cho API cần key:

```bash
-H "X-API-Key: YOUR_API_KEY"
```

Response chuẩn:

```json
{
  "success": true,
  "data": {}
}
```

> Ghi chú: các API `/api/veo/images*`, `/api/veo/videos/from-*`, `/api/veo/tasks*` hiện là Media API alias, có thể gọi trực tiếp theo code hiện tại. Các API project/admin cũ cần API key.

---

# 1. Health / Auth

## Health

```bash
curl -X GET "https://nathamedia.net/api/health"
```

Kết quả thành công:

```json
{
  "status": "ok"
}
```

## Kiểm tra API key

```bash
curl -X POST "https://nathamedia.net/api/check-api-key" \
  -H "Content-Type: application/json" \
  -d '{"api_key":"YOUR_API_KEY"}'
```

Kết quả thành công:

```json
{
  "success": true,
  "data": {
    "exists": true,
    "active": true
  }
}
```

## Thông tin user hiện tại

```bash
curl -X GET "https://nathamedia.net/api/me" \
  -H "X-API-Key: YOUR_API_KEY"
```

Kết quả thành công:

```json
{
  "success": true,
  "data": {
    "api_key": "user-abc...",
    "username": "demo",
    "role": "USER",
    "permissions": ["CREATE_VIDEO"],
    "status": "active"
  }
}
```

---

# 2. Google Flow token/project

## Lấy session theo API key

```bash
curl -X POST "https://nathamedia.net/api/veo/account-session" \
  -H "X-API-Key: YOUR_API_KEY"
```

## Alias truyền key trong body

```bash
curl -X POST "https://nathamedia.net/api/get-token-project" \
  -H "Content-Type: application/json" \
  -d '{"api_key":"YOUR_API_KEY"}'
```

Kết quả thành công:

```json
{
  "success": true,
  "data": {
    "account_name": "account@gmail.com",
    "token": "GOOGLE_ACCESS_TOKEN",
    "project_id": "GOOGLE_FLOW_PROJECT_ID"
  }
}
```

---

# 3. Upload ảnh reference

Upload ảnh trước, lấy `data.path` để truyền vào `image_paths`.

```bash
curl -X POST "https://nathamedia.net/api/veo/media/upload-reference" \
  -F "file=@./ref.jpg"
```

Kết quả thành công:

```json
{
  "success": true,
  "data": {
    "path": "D:/tool_veo_3/output/media_api/upload_abcd1234.jpg",
    "filename": "upload_abcd1234.jpg",
    "original_name": "ref.jpg"
  }
}
```

---

# 4. Tạo ảnh

## 4.1 Tạo ảnh từ 1 prompt

```bash
curl -X POST "https://nathamedia.net/api/veo/images" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "a cinematic cat wearing sunglasses, ultra realistic",
    "count": 4,
    "model": "NARWHAL",
    "screen_ratio": "16:9",
    "project_name": "Demo Images"
  }'
```

Kết quả thành công `202 Accepted`:

```json
{
  "success": true,
  "data": {
    "items": [
      {
        "id": "TASK_ID_1",
        "project_id": "PROJECT_ID",
        "name": "NARWHAL: a cinematic cat wearing sunglasses",
        "action_type": "CREATE_IMAGE",
        "model": "NARWHAL",
        "screen_ratio": "16:9",
        "prompts": ["a cinematic cat wearing sunglasses, ultra realistic"],
        "status": "PROCESSING"
      }
    ],
    "total": 4,
    "project_id": "PROJECT_ID"
  }
}
```

## 4.2 Tạo ảnh 4K

```bash
curl -X POST "https://nathamedia.net/api/veo/images/4k" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "luxury perfume product photo on black marble",
    "count": 2,
    "screen_ratio": "1:1"
  }'
```

Kết quả trả về giống `/api/veo/images`, task chạy async.

## 4.3 Tạo nhiều prompt một lần

```bash
curl -X POST "https://nathamedia.net/api/veo/images" \
  -H "Content-Type: application/json" \
  -d '{
    "prompts": [
      "prompt 1: futuristic city at night",
      "prompt 2: cute dog in astronaut suit"
    ],
    "count": 2,
    "project_name": "Batch Image Demo",
    "screen_ratio": "9:16"
  }'
```

Kết quả: tạo `len(prompts) * count` task.

## 4.4 Tạo ảnh dùng runtime token/proxy riêng

```bash
curl -X POST "https://nathamedia.net/api/veo/images" \
  -H "Content-Type: application/json" \
  -d '{
    "prompts": ["p1", "p2", "p3"],
    "count": 1,
    "runtime_tokens": [
      "GOOGLE_ACCESS_TOKEN_1",
      "GOOGLE_ACCESS_TOKEN_2"
    ],
    "runtime_proxies": [
      "http://user:pass@ip1:port",
      "http://user:pass@ip2:port"
    ]
  }'
```

Kết quả: scheduler dùng token/proxy truyền vào. Nếu không truyền `runtime_proxies`, server sẽ dùng `proxy.txt`.

---

# 5. Tạo ảnh từ ảnh reference

Có 4 cách truyền ảnh: `multipart file`, `image_paths` path server, `image_paths` URL, hoặc upload trước rồi dùng `data.path`.

## 5.1 Multipart file upload trực tiếp

```bash
curl -X POST "https://nathamedia.net/api/veo/images/from-images" \
  -F "prompt=turn this person into anime style" \
  -F "files=@./ref.jpg" \
  -F "count=2" \
  -F "screen_ratio=1:1"
```

## 5.2 JSON với local path trên server

```bash
curl -X POST "https://nathamedia.net/api/veo/images/from-images" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "make a premium studio portrait",
    "image_paths": ["D:/tool_veo_3/output/media_api/upload_abcd1234.jpg"],
    "count": 2,
    "screen_ratio": "1:1"
  }'
```

## 5.3 JSON với link ảnh public

```bash
curl -X POST "https://nathamedia.net/api/veo/images/from-images" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "change outfit to luxury fashion campaign",
    "image_paths": ["https://example.com/ref.jpg"],
    "count": 1,
    "screen_ratio": "9:16"
  }'
```

## 5.4 Ảnh từ base64

Endpoint Media alias không đọc trực tiếp field base64 cho ảnh reference. Cách đúng là dùng API cũ `/api/veo/create-image` với `image_refs_b64` hoặc upload trước.

```bash
IMG_B64=$(base64 -w 0 ./ref.jpg)

curl -X POST "https://nathamedia.net/api/veo/create-image" \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"project_id\": \"PROJECT_ID\",
    \"prompt\": \"turn this into anime style\",
    \"model\": \"NARWHAL\",
    \"aspect\": \"IMAGE_ASPECT_RATIO_SQUARE\",
    \"count\": 1,
    \"image_refs_b64\": [
      {\"b64\": \"$IMG_B64\", \"mime\": \"image/jpeg\", \"name\": \"ref.jpg\"}
    ]
  }"
```

Kết quả thành công `202 Accepted`:

```json
{
  "success": true,
  "data": {
    "id": "TASK_ID",
    "project_id": "PROJECT_ID",
    "action_type": "CREATE_IMAGE",
    "model": "NARWHAL",
    "status": "PENDING",
    "count": 1
  }
}
```

## 5.5 Tạo ảnh 4K từ ảnh

```bash
curl -X POST "https://nathamedia.net/api/veo/images/from-images/4k" \
  -F "prompt=upscale and enhance as luxury product photo" \
  -F "files=@./ref.jpg" \
  -F "count=1"
```

---

# 6. Tạo video

## 6.1 Video từ ảnh đầu - multipart file

```bash
curl -X POST "https://nathamedia.net/api/veo/videos/from-start-image" \
  -F "prompt=slow cinematic camera push in, soft light" \
  -F "files=@./start.jpg" \
  -F "model=FAST" \
  -F "screen_ratio=16:9" \
  -F "project_name=Video From Start Image"
```

Kết quả thành công `202 Accepted`:

```json
{
  "success": true,
  "data": {
    "items": [
      {
        "id": "TASK_ID",
        "project_id": "PROJECT_ID",
        "action_type": "IMAGE_TO_VIDEO",
        "model": "FAST",
        "screen_ratio": "16:9",
        "prompts": ["slow cinematic camera push in, soft light"],
        "status": "PROCESSING"
      }
    ],
    "total": 1,
    "project_id": "PROJECT_ID"
  }
}
```

## 6.2 Video từ ảnh reference - multipart file

```bash
curl -X POST "https://nathamedia.net/api/veo/videos/from-reference-image" \
  -F "prompt=subject walking in heavy rain, cinematic" \
  -F "files=@./ref.jpg" \
  -F "model=FAST" \
  -F "screen_ratio=9:16"
```

## 6.3 Video từ ảnh đầu + ảnh cuối - JSON path

```bash
curl -X POST "https://nathamedia.net/api/veo/videos/from-start-end-images" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "smooth transition from start image to end image",
    "image_paths": ["D:/tool_veo_3/output/media_api/start.jpg"],
    "end_image_paths": ["D:/tool_veo_3/output/media_api/end.jpg"],
    "model": "FAST",
    "screen_ratio": "16:9"
  }'
```

## 6.4 Video từ ảnh bằng link public

```bash
curl -X POST "https://nathamedia.net/api/veo/videos/from-start-image" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "a car driving through neon city",
    "image_paths": ["https://example.com/start.jpg"],
    "model": "FAST",
    "screen_ratio": "16:9"
  }'
```

## 6.5 Video từ ảnh base64 - single image mode

Dùng endpoint cũ `/api/veo/create-i2v`, hỗ trợ `images_b64` gồm `b64`, `url`, hoặc `path`.

```bash
IMG_B64=$(base64 -w 0 ./start.jpg)

curl -X POST "https://nathamedia.net/api/veo/create-i2v" \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"project_id\": \"PROJECT_ID\",
    \"project_name\": \"Base64 I2V Demo\",
    \"prompt\": \"slow camera movement, cinematic lighting\",
    \"model\": \"FAST\",
    \"screen_ratio\": \"16:9\",
    \"mode\": \"multi\",
    \"images_b64\": [
      {\"b64\": \"$IMG_B64\", \"mime\": \"image/jpeg\", \"name\": \"start.jpg\"}
    ]
  }"
```

Kết quả thành công `202 Accepted`:

```json
{
  "success": true,
  "data": {
    "id": "TASK_ID",
    "project_id": "PROJECT_ID",
    "name": "I2V #1.1: start.jpg",
    "action_type": "IMAGE_TO_VIDEO",
    "model": "FAST",
    "screen_ratio": "16:9",
    "status": "PENDING"
  }
}
```

## 6.6 Video từ URL ảnh bằng `/api/veo/create-i2v`

```bash
curl -X POST "https://nathamedia.net/api/veo/create-i2v" \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "project_id": "PROJECT_ID",
    "project_name": "URL I2V Demo",
    "prompt": "make the subject smile and turn around",
    "mode": "multi",
    "images_b64": [
      {"url": "https://example.com/start.jpg", "name": "start.jpg"}
    ]
  }'
```

## 6.7 Video từ local path bằng `/api/veo/create-i2v`

```bash
curl -X POST "https://nathamedia.net/api/veo/create-i2v" \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "project_id": "PROJECT_ID",
    "prompt": "cinematic motion",
    "mode": "multi",
    "images_b64": [
      {"path": "D:/tool_veo_3/output/media_api/start.jpg", "mime": "image/jpeg", "name": "start.jpg"}
    ]
  }'
```

## 6.8 Video start/end bằng base64 - frames mode

```bash
START_B64=$(base64 -w 0 ./start.jpg)
END_B64=$(base64 -w 0 ./end.jpg)

curl -X POST "https://nathamedia.net/api/veo/create-i2v" \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"project_id\": \"PROJECT_ID\",
    \"prompt\": \"smooth transition from first image to second image\",
    \"mode\": \"frames\",
    \"model\": \"FAST\",
    \"screen_ratio\": \"16:9\",
    \"images_b64\": [
      {\"b64\": \"$START_B64\", \"mime\": \"image/jpeg\", \"name\": \"start.jpg\"},
      {\"b64\": \"$END_B64\", \"mime\": \"image/jpeg\", \"name\": \"end.jpg\"}
    ]
  }"
```

## 6.9 Nhiều prompt cho cùng ảnh

```bash
curl -X POST "https://nathamedia.net/api/veo/create-i2v" \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "project_id": "PROJECT_ID",
    "prompt": "motion prompt 1\nmotion prompt 2\nmotion prompt 3",
    "mode": "multi",
    "images_b64": [
      {"url": "https://example.com/start.jpg", "name": "start.jpg"}
    ]
  }'
```

Kết quả: tạo 1 task cho mỗi dòng prompt.

---

# 7. Xem task và lấy kết quả đầu ra

## Danh sách task

```bash
curl -X GET "https://nathamedia.net/api/veo/tasks?limit=100"
```

Lọc theo project/status:

```bash
curl -X GET "https://nathamedia.net/api/veo/tasks?project_id=PROJECT_ID&status=COMPLETED&limit=100"
```

## Chi tiết task

```bash
curl -X GET "https://nathamedia.net/api/veo/tasks/TASK_ID"
```

Kết quả khi đang chạy:

```json
{
  "success": true,
  "data": {
    "id": "TASK_ID",
    "project_id": "PROJECT_ID",
    "status": "PROCESSING",
    "media_id": null,
    "output_filename": null,
    "error": null
  }
}
```

Kết quả khi hoàn thành ảnh/video:

```json
{
  "success": true,
  "data": {
    "id": "TASK_ID",
    "project_id": "PROJECT_ID",
    "status": "COMPLETED",
    "media_id": "https://flow-content.google/...",
    "output_filename": "D:/tool_veo_3/output/media_api/PROJECT_ID_001.jpg",
    "raw_result": {
      "engine": "reference-implementation",
      "result": {
        "status": "completed",
        "download_url": "https://flow-content.google/...",
        "output_path": "D:/tool_veo_3/output/media_api/PROJECT_ID_001.jpg"
      }
    }
  }
}
```

## Tải ảnh theo task

```bash
curl -L "https://nathamedia.net/api/veo/image/TASK_ID?idx=0" \
  -H "X-API-Key: YOUR_API_KEY" \
  -o output.jpg
```

## Tải ảnh reference đã dùng

```bash
curl -L "https://nathamedia.net/api/veo/ref-image/TASK_ID?idx=0" \
  -H "X-API-Key: YOUR_API_KEY" \
  -o ref.jpg
```

## Tải toàn bộ project dạng zip

```bash
curl -L "https://nathamedia.net/api/veo/project/PROJECT_ID/download" \
  -o project_outputs.zip
```

---

# 8. Project

## Danh sách project

```bash
curl -X GET "https://nathamedia.net/api/veo/projects" \
  -H "X-API-Key: YOUR_API_KEY"
```

## Tạo project

```bash
curl -X POST "https://nathamedia.net/api/veo/project" \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"My Project"}'
```

Kết quả thành công:

```json
{
  "success": true,
  "data": {
    "id": "PROJECT_ID",
    "name": "My Project"
  }
}
```

## Chi tiết project

```bash
curl -X GET "https://nathamedia.net/api/veo/project/PROJECT_ID" \
  -H "X-API-Key: YOUR_API_KEY"
```

## Xoá project

```bash
curl -X DELETE "https://nathamedia.net/api/veo/project/PROJECT_ID" \
  -H "X-API-Key: YOUR_API_KEY"
```

## Batch tạo task từ prompts

```bash
curl -X POST "https://nathamedia.net/api/veo/project/batch-create" \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Batch Image Project",
    "prompts": ["p1", "p2"],
    "model": "NARWHAL",
    "screen_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT",
    "videos_per_prompt": 1
  }'
```

## Import kiểu Excel JSON

```bash
curl -X POST "https://nathamedia.net/api/veo/project/import-excel" \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Import Project",
    "model": "NARWHAL",
    "tasks": [
      {"prompt": "p1", "image_paths": []},
      {"prompt": "p2", "image_paths": ["https://example.com/ref.jpg"]}
    ]
  }'
```

---

# 9. Retry / pause / resume

```bash
curl -X POST "https://nathamedia.net/api/veo/project/PROJECT_ID/retry-failed" \
  -H "X-API-Key: YOUR_API_KEY"

curl -X POST "https://nathamedia.net/api/veo/project/PROJECT_ID/retry-failed-today" \
  -H "X-API-Key: YOUR_API_KEY"

curl -X POST "https://nathamedia.net/api/veo/project/PROJECT_ID/resume-paused" \
  -H "X-API-Key: YOUR_API_KEY"

curl -X POST "https://nathamedia.net/api/veo/project/PROJECT_ID/pause-pending" \
  -H "X-API-Key: ADMIN_API_KEY"
```

Kết quả thành công mẫu:

```json
{
  "success": true,
  "data": {
    "retried": 10,
    "message": "Đã đưa 10 task vào hàng chờ thử lại"
  }
}
```

---

# 10. Queue / logs admin

## Xem queue settings

```bash
curl -X GET "https://nathamedia.net/api/veo/queue/settings"
```

## Cập nhật max concurrent

```bash
curl -X PUT "https://nathamedia.net/api/veo/queue/settings" \
  -H "X-API-Key: ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"max_concurrent":100}'
```

## Monitor queue

```bash
curl -X GET "https://nathamedia.net/api/admin/queue/monitor"
```

## Tail logs

```bash
curl -X GET "https://nathamedia.net/api/admin/logs/tail?lines=200&contains=401" \
  -H "X-API-Key: ADMIN_API_KEY"
```

---

# 11. User job results

## Ghi kết quả job ngoài

```bash
curl -X POST "https://nathamedia.net/api/user-job-results" \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '[
    {
      "project_name": "Demo",
      "job_id": "job-1",
      "prompt": "p1",
      "status": "COMPLETED",
      "download_url": "https://example.com/output.jpg",
      "duration_seconds": 60
    }
  ]'
```

## List kết quả job ngoài

```bash
curl -X GET "https://nathamedia.net/api/user-job-results?limit=100&status=COMPLETED" \
  -H "X-API-Key: YOUR_API_KEY"
```

---

# 12. Admin users

## Login admin

```bash
curl -X POST "https://nathamedia.net/api/admin/login" \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"password"}'
```

## List users

```bash
curl -X GET "https://nathamedia.net/api/admin/users" \
  -H "X-API-Key: ADMIN_API_KEY"
```

## Tạo user

```bash
curl -X POST "https://nathamedia.net/api/admin/users" \
  -H "X-API-Key: ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "username":"user1",
    "password":"123456",
    "role":"USER",
    "permissions":["CREATE_VIDEO"],
    "is_active":true
  }'
```

## Update user

```bash
curl -X PUT "https://nathamedia.net/api/admin/users/USER_ID" \
  -H "X-API-Key: ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"is_active":true,"permissions":["CREATE_VIDEO"]}'
```

## Delete user

```bash
curl -X DELETE "https://nathamedia.net/api/admin/users/USER_ID" \
  -H "X-API-Key: ADMIN_API_KEY"
```

---

# 13. Field quan trọng

## Tạo ảnh `/api/veo/images*`

| Field | Kiểu | Ghi chú |
|---|---:|---|
| `prompt` | string | 1 prompt |
| `prompts` | array/string | nhiều prompt hoặc text nhiều dòng |
| `count` | number | số ảnh mỗi prompt, tối đa 100 ở Media API |
| `model` | string | mặc định `NARWHAL` |
| `screen_ratio` | string | `16:9`, `9:16`, `1:1` |
| `project_id` | string | dùng project có sẵn |
| `project_name` | string | tạo project mới nếu thiếu `project_id` |
| `image_paths` | array | path hoặc URL ảnh reference |
| `runtime_tokens` | array | access token runtime |
| `runtime_proxies` | array | proxy tương ứng token |

## Tạo video `/api/veo/create-i2v`

| Field | Kiểu | Ghi chú |
|---|---:|---|
| `project_id` | string | bắt buộc |
| `project_name` | string | tạo project nếu `project_id` chưa có |
| `prompt` | string | có thể nhiều dòng, mỗi dòng tạo task |
| `images_b64` | array | bắt buộc, nhận `{b64}` hoặc `{url}` hoặc `{path}` |
| `mode` | string | `multi` = mỗi ảnh 1 video, `frames` = start/end |
| `model` | string | mặc định `FAST` |
| `screen_ratio` | string | `16:9`, `9:16`, `1:1` |
| `veo_cookie` | string | optional token/cookie riêng |
| `proxy_url` | string | optional proxy riêng |

## Status task

| Status | Ý nghĩa |
|---|---|
| `PENDING` | đã tạo, chờ chạy |
| `PROCESSING` | đang chạy |
| `COMPLETED` | hoàn thành, xem `media_id`/`output_filename` |
| `FAILED` / `ERROR` | lỗi, xem `error` |
| `PAUSED` | tạm dừng |
