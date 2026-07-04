#!/usr/bin/env bash
# =============================================================================
# deploy.sh — Script deploy web lên VPS Linux
# Chạy một lần duy nhất sau khi đã upload code lên server.
#
# Cách dùng:
#   1. Upload toàn bộ project lên VPS:
#       scp -r d:\tool_veo_3 YOUR_USER@YOUR_VPS_IP:/home/YOUR_USER/
#   2. SSH vào VPS và chạy:
#       bash /home/YOUR_USER/tool_veo_3/deploy/deploy.sh
# =============================================================================
set -e  # Dừng script ngay khi có lỗi

# ─── Biến cấu hình — SỬA NHỮNG DÒNG NÀY ─────────────────────────────────────
APP_USER="YOUR_USER"
APP_DIR="/home/${APP_USER}/tool_veo_3"
VENV_DIR="${APP_DIR}/venv"
DOMAIN="api.yourdomain.com"      # Domain của bạn
NGINX_CONF_TARGET="/etc/nginx/sites-available/web"
SERVICE_TARGET="/etc/systemd/system/web.service"
LOG_DIR="/var/log/web"
# ─────────────────────────────────────────────────────────────────────────────

echo "======================================================"
echo "  VietAuto API — Deploy Script"
echo "  App dir : ${APP_DIR}"
echo "  Domain  : ${DOMAIN}"
echo "======================================================"

# ─── 1. Kiểm tra phụ thuộc hệ thống ─────────────────────────────────────────
echo ""
echo "[1/8] Cài đặt phụ thuộc hệ thống..."
sudo apt-get update -qq
sudo apt-get install -y python3 python3-pip python3-venv nginx certbot python3-certbot-nginx

# ─── 2. Tạo virtualenv ───────────────────────────────────────────────────────
echo ""
echo "[2/8] Tạo Python virtualenv..."
if [ ! -d "${VENV_DIR}" ]; then
    python3 -m venv "${VENV_DIR}"
    echo "  → Đã tạo venv mới tại ${VENV_DIR}"
else
    echo "  → venv đã tồn tại, bỏ qua."
fi

# ─── 3. Cài pip packages ─────────────────────────────────────────────────────
echo ""
echo "[3/8] Cài đặt Python packages vào venv..."
"${VENV_DIR}/bin/pip" install --upgrade pip -q
"${VENV_DIR}/bin/pip" install flask gunicorn gevent werkzeug pymongo curl_cffi -q
# Cài các package từ requirements.txt nếu tồn tại
if [ -f "${APP_DIR}/requirements.txt" ]; then
    "${VENV_DIR}/bin/pip" install -r "${APP_DIR}/requirements.txt" -q
    echo "  → Đã cài từ requirements.txt"
fi

# ─── 4. Tạo thư mục log ──────────────────────────────────────────────────────
echo ""
echo "[4/8] Tạo thư mục log..."
sudo mkdir -p "${LOG_DIR}"
sudo chown -R "${APP_USER}:www-data" "${LOG_DIR}"
sudo chmod 775 "${LOG_DIR}"

# ─── 5. Cài đặt systemd service ──────────────────────────────────────────────
echo ""
echo "[5/8] Cài đặt systemd service..."
# Thay thế placeholder YOUR_USER trong file service
sed "s/YOUR_USER/${APP_USER}/g" "${APP_DIR}/deploy/web.service" \
    | sudo tee "${SERVICE_TARGET}" > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable web
echo "  → Service đã được cài tại ${SERVICE_TARGET}"

# ─── 6. Cài đặt Nginx config ─────────────────────────────────────────────────
echo ""
echo "[6/8] Cài đặt Nginx config..."
# Thay placeholder trong nginx.conf
sed "s/api.yourdomain.com/${DOMAIN}/g; s/YOUR_USER/${APP_USER}/g" \
    "${APP_DIR}/deploy/nginx.conf" \
    | sudo tee "${NGINX_CONF_TARGET}" > /dev/null
# Bật site
sudo ln -sf "${NGINX_CONF_TARGET}" /etc/nginx/sites-enabled/web
sudo nginx -t
sudo systemctl reload nginx
echo "  → Nginx đã được cấu hình cho domain: ${DOMAIN}"

# ─── 7. Cấp SSL với Let's Encrypt ────────────────────────────────────────────
echo ""
echo "[7/8] Cấp SSL certificate (Let's Encrypt)..."
echo "  Nếu domain chưa trỏ đúng về IP VPS, bước này sẽ thất bại."
echo "  Nhấn Enter để tiếp tục hoặc Ctrl+C để bỏ qua và cấp SSL sau."
read -p "  Email của bạn (để Let's Encrypt thông báo): " LE_EMAIL
if [ -n "${LE_EMAIL}" ]; then
    sudo certbot --nginx -d "${DOMAIN}" --non-interactive \
        --agree-tos --email "${LE_EMAIL}" --redirect || \
    echo "  [!] SSL thất bại. Hãy cấp thủ công sau: sudo certbot --nginx -d ${DOMAIN}"
fi

# ─── 8. Khởi động service ────────────────────────────────────────────────────
echo ""
echo "[8/8] Khởi động web service..."
sudo systemctl start web
sudo systemctl status web --no-pager

echo ""
echo "======================================================"
echo "  ✅ Deploy hoàn tất!"
echo "  API URL  : https://${DOMAIN}"
echo "  Health   : https://${DOMAIN}/api/health"
echo ""
echo "  Các lệnh quản lý:"
echo "    sudo systemctl status  web   # Xem trạng thái"
echo "    sudo systemctl restart web   # Restart"
echo "    sudo systemctl stop    web   # Dừng"
echo "    sudo journalctl -u web -f    # Xem log realtime"
echo "======================================================"
