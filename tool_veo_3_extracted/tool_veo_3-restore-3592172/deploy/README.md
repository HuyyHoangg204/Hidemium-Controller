# Deploy VietAuto API lên VPS

## Các file trong thư mục này

| File | Mục đích |
|------|----------|
| `gunicorn.conf.py` | Config Gunicorn (production WSGI server) |
| `web.service` | Systemd service — tự restart khi crash/reboot |
| `nginx.conf` | Nginx reverse-proxy + SSL + rate limiting |
| `deploy.sh` | Script tự động hoá toàn bộ bước trên |
| `.env.example` | Mẫu biến môi trường |

## Các bước deploy

### Bước 1 — Chuẩn bị `.env`
```bash
cp deploy/.env.example .env
nano .env   # Điền VIETAUTO_API_KEY, MONGO_URI, ...
```

### Bước 2 — Upload lên VPS
```bash
# Chạy trên máy Windows của bạn
scp -r d:\tool_veo_3 YOUR_USER@YOUR_VPS_IP:/home/YOUR_USER/
```

### Bước 3 — Sửa biến trong deploy.sh
```bash
# Trên VPS, mở file và sửa 3 dòng đầu:
nano /home/YOUR_USER/tool_veo_3/deploy/deploy.sh
```
Sửa:
- `APP_USER` → tên user Linux của bạn
- `APP_DIR` → đường dẫn thực tế
- `DOMAIN` → domain (ví dụ `api.example.com`)

### Bước 4 — Chạy deploy
```bash
bash /home/YOUR_USER/tool_veo_3/deploy/deploy.sh
```

Script sẽ tự động:
1. Cài Nginx, Python, Certbot
2. Tạo virtualenv + cài packages
3. Cài systemd service (auto-start)
4. Cấu hình Nginx với domain
5. Cấp SSL từ Let's Encrypt (miễn phí)
6. Khởi động server

### Sau khi deploy xong
```bash
# Kiểm tra
curl https://api.yourdomain.com/api/health
# → {"status": "ok"}

# Quản lý service
sudo systemctl status  web
sudo systemctl restart web
sudo journalctl -u web -f   # log realtime
```

## Dùng API với API Key

```bash
curl -H "Authorization: Bearer YOUR_API_KEY" \
     https://api.yourdomain.com/api/me
```

## Cấu trúc API
- `GET /api/health` — Health check (không cần key)
- `GET /api/me` — Thông tin user
- `POST /api/veo/project` — Tạo project
- `POST /api/veo/project/batch-create` — Tạo hàng loạt video
- `GET /api/veo/projects` — Danh sách projects
- `GET /api/docs` — Tài liệu API đầy đủ
