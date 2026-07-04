# Hidemium Controller

Folder app riêng để điều khiển **Hidemium API Automation V4** qua local API mặc định `http://127.0.0.1:2222`.

## 1) Chuẩn bị

- Mở Hidemium 4 trước.
- Trong Hidemium cần bật API/Automation nếu app có mục setting tương ứng.
- Copy `.env.example` thành `.env` nếu muốn đổi cấu hình.

```env
HIDEMIUM_BASE_URL=http://127.0.0.1:2222
HIDEMIUM_IS_LOCAL=false
REQUEST_TIMEOUT=30
```

> `HIDEMIUM_IS_LOCAL=false`: profile cloud.  
> `HIDEMIUM_IS_LOCAL=true`: profile local.

## 2) Chạy bằng Python

```bat
cd /d "d:\New folder\tool_veo_3\hidemium_controller"
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py list --limit 5
```

## 3) Chạy giao diện bảng

```bat
python gui.py
```

Trong giao diện:

- Bấm **Tải danh sách** để load profile.
- Click chọn một profile trong bảng.
- Bấm **Mở profile đã chọn** hoặc double-click dòng profile.
- Bấm **Đóng profile đã chọn** để đóng profile đó.
- Có thể đổi `Base URL`, bật `Profile local`, nhập `Search`, `Limit` ngay trên giao diện.
- Khung **Console log** phía dưới sẽ hiện quá trình gọi API, mở/đóng profile, lỗi và kết quả.
- Log đầy đủ cũng được ghi vào folder `logs\hidemium_controller_YYYYMMDD.log`.

## 4) Build exe

Chạy:

```bat
build_exe.bat
```

File exe sẽ nằm ở:

```text
dist\hidemium_controller.exe  Giao diện bảng + console log + file log
```

Mở app:

```bat
dist\hidemium_controller.exe
```

Log đầy đủ sẽ nằm ở:

```text
dist\logs\hidemium_controller_YYYYMMDD.log
```

## 5) Lệnh thường dùng cho CLI

### Kiểm tra token/user uuid

```bat
dist\hidemiumctl.exe user-uuid
```

### Liệt kê profile

```bat
dist\hidemiumctl.exe list --limit 20
```

Profile local:

```bat
dist\hidemiumctl.exe list --local --limit 20
```

### Lấy chi tiết profile

```bat
dist\hidemiumctl.exe get PROFILE_UUID
```

### Mở profile

```bat
dist\hidemiumctl.exe open PROFILE_UUID
```

Mở với kích thước/cửa sổ riêng:

```bat
dist\hidemiumctl.exe open PROFILE_UUID --command "--window-position=500,500 --window-size=1280,800"
```

Mở với proxy:

```bat
dist\hidemiumctl.exe open PROFILE_UUID --proxy "HTTP|117.7.228.236|60001|user|pass"
```

### Đóng profile

```bat
dist\hidemiumctl.exe close PROFILE_UUID
```

### Gỡ proxy khỏi profile

```bat
dist\hidemiumctl.exe proxy-remove PROFILE_UUID
```

### Lấy version/status/tag/default config

```bat
dist\hidemiumctl.exe versions
dist\hidemiumctl.exe statuses
dist\hidemiumctl.exe tags
dist\hidemiumctl.exe configs
```

### Tạo profile từ JSON

Tạo file `profile.json`, rồi chạy:

```bat
dist\hidemiumctl.exe create-custom --json profile.json --local
```

Hoặc truyền JSON trực tiếp:

```bat
dist\hidemiumctl.exe create-custom --json "{\"name\":\"test profile\"}" --local
```

## 5) Endpoint đã map từ docs

- `GET /openProfile?uuid=...`
- `GET /closeProfile?uuid=...`
- `GET /authorize?uuid=...`
- `POST /v1/browser/list?is_local=...`
- `GET /v2/browser/get-profile-by-uuid/{uuid}`
- `POST /create-profile-by-default?is_local=...`
- `POST /create-profile-custom?is_local=...`
- `DELETE /v1/browser/destroy?is_local=...`
- `PUT /v2/proxy/quick-edit?is_local=...`
- `GET /user-settings/token`
- `GET /v2/status-profile`
- `GET /v2/tag`
- `GET /v2/default-config`
- `GET /v2/browser/get-list-version`
