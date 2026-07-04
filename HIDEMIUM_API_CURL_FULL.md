# Hidemium API V4 - cURL đầy đủ cho profile, campaign, schedule

Base URL mặc định:

```text
http://127.0.0.1:2222
```

Quy ước:

- `is_local=false`: profile cloud.
- `is_local=true`: profile local.
- Thay các giá trị `PROFILE_UUID`, `FOLDER_UUID`, `CAMPAIGN_ID`, `SCHEDULE_ID` bằng dữ liệu thật.

---

## 1) Profile - danh sách / lấy chi tiết / mở / đóng

### List profile

```bash
curl --location 'http://127.0.0.1:2222/v1/browser/list?is_local=false' \
  --header 'Content-Type: application/json' \
  --data '{
    "orderName": 0,
    "orderLastOpen": 0,
    "page": 1,
    "limit": 50,
    "search": "",
    "status": "",
    "date_range": ["", ""],
    "folder_id": []
  }'
```

### Lấy profile theo UUID

```bash
curl --location 'http://127.0.0.1:2222/v2/browser/get-profile-by-uuid/PROFILE_UUID?is_local=false'
```

### Mở profile

```bash
curl --location 'http://127.0.0.1:2222/openProfile?uuid=PROFILE_UUID&command=--window-position=100,100%20--window-size=1280,800'
```

### Mở profile kèm proxy

```bash
curl --location 'http://127.0.0.1:2222/openProfile?uuid=PROFILE_UUID&command=--window-position=100,100%20--window-size=1280,800&proxy=HTTP|host|port|user|pass'
```

### Đóng profile

```bash
curl --location 'http://127.0.0.1:2222/closeProfile?uuid=PROFILE_UUID'
```

### Check authorize profile

```bash
curl --location 'http://127.0.0.1:2222/authorize?uuid=PROFILE_UUID'
```

---

## 2) Profile - thêm / sửa / xóa

### Tạo profile bằng default config

```bash
curl --location 'http://127.0.0.1:2222/create-profile-by-default?is_local=false' \
  --header 'Content-Type: application/json' \
  --data '{
    "defaultConfigId": 576
  }'
```

### Tạo profile custom

```bash
curl --location 'http://127.0.0.1:2222/create-profile-custom?is_local=true' \
  --header 'Content-Type: application/json' \
  --data '{
    "os": "win",
    "device_type": "",
    "device": [],
    "osVersion": "10",
    "browser": "chrome",
    "version": "121",
    "userAgent": "",
    "canvas": true,
    "webGLImage": "false",
    "audioContext": "false",
    "webGLMetadata": "false",
    "webGLVendor": "",
    "webGLMetadataRenderer": "",
    "clientRectsEnable": "false",
    "noiseFont": "false",
    "language": "vi-VN",
    "deviceMemory": 4,
    "hardwareConcurrency": 8,
    "resolution": "1280x800",
    "StartURL": "https://hidemium.io/",
    "command": "--lang=vi",
    "name": "Profile test",
    "folder_name": "Auto Profiles"
  }'
```

### Sửa tên profile

```bash
curl --location --request PUT 'http://127.0.0.1:2222/v2/browser/update-once?is_local=false' \
  --header 'Content-Type: application/json' \
  --data '{
    "column": "name",
    "profile_uuid": "PROFILE_UUID",
    "data": "Tên profile mới"
  }'
```

### Sửa ghi chú profile

```bash
curl --location --request PUT 'http://127.0.0.1:2222/v2/browser/update-note?is_local=false' \
  --header 'Content-Type: application/json' \
  --data '{
    "note": "Ghi chú mới",
    "profile_uuid": "PROFILE_UUID"
  }'
```

### Đổi fingerprint profile

```bash
curl --location --request PUT 'http://127.0.0.1:2222/v2/browser/change-fingerprint?is_local=false' \
  --header 'Content-Type: application/json' \
  --data '{
    "profile_uuid": "PROFILE_UUID"
  }'
```

### Xóa profile

> Theo Postman docs, body dùng key `uuid_browser`.

```bash
curl --location --request DELETE 'http://127.0.0.1:2222/v1/browser/destroy?is_local=false' \
  --header 'Content-Type: application/json' \
  --data '{
    "uuid_browser": [
      "PROFILE_UUID_1",
      "PROFILE_UUID_2"
    ]
  }'
```

---

## 3) Status / tag / folder / proxy

### List status

```bash
curl --location 'http://127.0.0.1:2222/v2/status-profile?is_local=true'
```

### Đổi status profile

Status mặc định theo docs:

- `0`: No Status
- `-1`: Ban
- `-2`: Ready
- `-3`: New

```bash
curl --location --request PUT 'http://127.0.0.1:2222/v2/status-profile/change-status?is_local=true' \
  --header 'Content-Type: application/json' \
  --data '{
    "browser_uuid": "PROFILE_UUID",
    "id": -2
  }'
```

### List tag

```bash
curl --location 'http://127.0.0.1:2222/v2/tag?is_local=true'
```

### Gán/sync tag cho profile

```bash
curl --location 'http://127.0.0.1:2222/v2/tag?is_local=true' \
  --header 'Content-Type: application/json' \
  --data '{
    "tags": ["tag 1", "tag 2"],
    "profile_uuid": "PROFILE_UUID"
  }'
```

### List folder

```bash
curl --location 'http://127.0.0.1:2222/v1/folder/list?is_local=false&page=1&limit=50'
```

### Add profile vào folder

```bash
curl --location 'http://127.0.0.1:2222/v1/folder/FOLDER_UUID/add-browser?is_local=true' \
  --header 'Content-Type: application/json' \
  --data '{
    "uuid_browser": ["PROFILE_UUID"]
  }'
```

### Sửa proxy nhanh cho profile

```bash
curl --location --request PUT 'http://127.0.0.1:2222/v2/proxy/quick-edit?is_local=true' \
  --header 'Content-Type: application/json' \
  --data '{
    "type": "HTTP",
    "port": "8110",
    "user": "username",
    "pass": "password",
    "checker": "ipscore.io",
    "checkBeforeStart": true,
    "ip": "45.94.47.66",
    "browser_uuid": "PROFILE_UUID",
    "details": {},
    "status": "NEW"
  }'
```

### Gỡ proxy khỏi profile

```bash
curl --location --request PUT 'http://127.0.0.1:2222/v2/proxy/quick-edit?is_local=false' \
  --header 'Content-Type: application/json' \
  --data '{
    "browser_uuid": "PROFILE_UUID",
    "id": -1
  }'
```

### Update proxy hàng loạt

```bash
curl --location 'http://127.0.0.1:2222/v2/browser/proxy/update?is_local=false' \
  --header 'Content-Type: application/json' \
  --data '{
    "browser_update": [
      {
        "uuid": "PROFILE_UUID_1",
        "proxy": "HTTP|1.1.1.1|1234|user|pass"
      },
      {
        "uuid": "PROFILE_UUID_2",
        "proxy": "SOCKS5|2.2.2.2|5678|user|pass"
      }
    ]
  }'
```

---

## 4) Config / script / version

### List default config

```bash
curl --location 'http://127.0.0.1:2222/v2/default-config?page=1&limit=10'
```

### List browser version

```bash
curl --location 'http://127.0.0.1:2222/v2/browser/get-list-version'
```

### List automation script

```bash
curl --location 'http://127.0.0.1:2222/v2/automation/script?page=1&limit=10'
```

### Get user uuid/token

```bash
curl --location 'http://127.0.0.1:2222/user-settings/token'
```

---

## 5) Campaign automation

### List campaign

```bash
curl --location 'http://127.0.0.1:2222/automation/campaign?search=&page=1&limit=10'
```

### Tạo campaign

> Body campaign có thể khác theo version/script. Dùng UI Hidemium tạo 1 campaign mẫu rồi copy cấu trúc nếu cần chính xác tuyệt đối.

```bash
curl --location 'http://127.0.0.1:2222/automation/campaign' \
  --header 'Content-Type: application/json' \
  --data '{
    "name": "Auto Campaign Test",
    "script_id": 1,
    "description": "Campaign tạo từ API"
  }'
```

### Add profile vào campaign

```bash
curl --location 'http://127.0.0.1:2222/automation/campaign/save-campaign-profile' \
  --header 'Content-Type: application/json' \
  --data '{
    "campaignId": "CAMPAIGN_ID",
    "profileIds": ["PROFILE_UUID_1", "PROFILE_UUID_2"]
  }'
```

### Update input variable của campaign

```bash
curl --location 'http://127.0.0.1:2222/automation/campaign/save-auto-campaign' \
  --header 'Content-Type: application/json' \
  --data '{
    "campaignId": "CAMPAIGN_ID",
    "input": [
      {
        "id": "80cefb86-e28c-4528-a07c-d04f7423ee70",
        "value": "row_index",
        "label": "row_index",
        "data": "",
        "name": ""
      },
      {
        "id": "9937a16d-c090-4370-a95b-9a6d233f9598",
        "value": "email",
        "label": "email",
        "data": "",
        "name": ""
      },
      {
        "id": "d6ec5deb-3377-4f51-9f01-145a7cdfb6a2",
        "label": "file_xlsx_path",
        "value": "file_xlsx_path",
        "data": "D:\\Download\\test.xlsx"
      }
    ],
    "settings": {
      "automationProcess": 10,
      "delayOpen": 2,
      "isScreenArrangement": true,
      "arrangementLayoutCol": 2,
      "arrangementLayoutRow": 3,
      "autoScaleScreen": true,
      "screenScalePercent": 25,
      "isSetWindowSize": false,
      "screenSize": "1280x1030",
      "profilePerWorker": 10,
      "writeLogs": true,
      "isEnableStealthPlugin": false,
      "isNotOverlapProfile": false,
      "closeProfileOnComplete": true
    }
  }'
```

### Set campaign variables

```bash
curl --location 'http://127.0.0.1:2222/automation/campaign/update-variables' \
  --header 'Content-Type: application/json' \
  --data '{
    "campaign_id": 306,
    "variables": [
      {
        "name": "variable_name",
        "value": "Variable value"
      }
    ]
  }'
```

### Xóa toàn bộ profile trong campaign

```bash
curl --location --request DELETE 'http://127.0.0.1:2222/automation/campaign/delete-all-campaign-profile' \
  --header 'Content-Type: application/json' \
  --data '{
    "campaignId": "CAMPAIGN_ID"
  }'
```

### Xóa campaign

```bash
curl --location 'http://127.0.0.1:2222/automation/delete-campaign' \
  --header 'Content-Type: application/json' \
  --data '{
    "ids": [CAMPAIGN_ID]
  }'
```

---

## 6) Schedule / hẹn giờ automation

### List schedule theo campaign

```bash
curl --location 'http://127.0.0.1:2222/automation/schedule?campaign_id=CAMPAIGN_ID&page=1&limit=10'
```

### Tạo schedule

> Docs/Postman không mô tả rõ toàn bộ field schedule cho mọi version. Mẫu dưới đây là khung để app gửi được JSON. Nếu Hidemium version của bạn yêu cầu field khác, hãy tạo schedule mẫu trong UI rồi bấm DevTools/Network hoặc gửi response lỗi cho mình map lại chính xác.

```bash
curl --location 'http://127.0.0.1:2222/automation/schedule' \
  --header 'Content-Type: application/json' \
  --data '{
    "campaign_id": CAMPAIGN_ID,
    "name": "Chạy campaign lúc 23h",
    "type": "once",
    "time": "2026-05-15 23:00:00",
    "timezone": "Asia/Ho_Chi_Minh",
    "status": true
  }'
```

### Bật/tắt schedule

```bash
curl --location --request PUT 'http://127.0.0.1:2222/automation/update-schedule-status' \
  --header 'Content-Type: application/json' \
  --data '{
    "id": SCHEDULE_ID,
    "status": true
  }'
```

### Xóa schedule

```bash
curl --location 'http://127.0.0.1:2222/automation/delete-schedule' \
  --header 'Content-Type: application/json' \
  --data '{
    "ids": [SCHEDULE_ID]
  }'
```
