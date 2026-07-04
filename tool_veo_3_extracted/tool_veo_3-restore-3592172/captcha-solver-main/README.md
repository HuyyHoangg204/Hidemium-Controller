## Description
Project này dùng để giải captcha veo3 bằng cách chạy một browser (chrome) thật và cài một extension để nhận request. Vì vậy project sẽ gồm 2 thành phần:
- Server: nhận request giải captcha
- Extension: kết nối socket tới server để thực hiện giải captcha và gửi kết quả trả về server

> **⚠️ QUAN TRỌNG**: Để đảm bảo giải captcha ổn định, cần sử dụng proxy xoay (nên ít nhất 15 giây xoay một lần). Bạn có thể setup proxy global cho toàn máy hoặc dùng extension để thêm proxy.

## Các bước thực hiện

### Cài extension
1. Mở Chrome
2. Vào phần quản lý extensions (chrome://extensions/)
3. Bật chế độ "Developer mode"
4. Click "Load unpacked" và chọn thư mục `src/modules/captcha/extension`

### Project setup

```bash
# cài đặt thư viện cần thiết
$ npm install

# build project
$ npm run build

# chạy server
$ npm run start:prod
```

Server sẽ khởi động trên port 3000 (hoặc port được cấu hình trong biến môi trường). Bạn có thể truy cập `http://localhost:3000/api/docs` để xem API documentation và test các endpoint.