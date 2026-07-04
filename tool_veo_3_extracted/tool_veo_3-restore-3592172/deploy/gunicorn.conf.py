"""
Gunicorn production configuration cho web.
File này được sử dụng khi chạy trực tiếp trên VPS Linux.

Chạy:
    gunicorn -c gunicorn.conf.py "web.run:app"
"""

import multiprocessing
import os

# ─── Workers ───────────────────────────────────────────────────────────────────
# Số worker = (2 × CPU_cores) + 1  — quy tắc chuẩn của Gunicorn
workers = multiprocessing.cpu_count() * 2 + 1

# Worker class: gevent cho async I/O (tốt cho long-polling vô hạn)
# Cài thêm: pip install gevent
worker_class = "gevent"

# Số connections tối đa mỗi worker (chỉ áp dụng cho gevent/eventlet)
worker_connections = 1000

# ─── Binding ───────────────────────────────────────────────────────────────────
# Gunicorn lắng nghe trên cổng 8080 (Nginx sẽ reverse-proxy sang đây)
bind = "127.0.0.1:8080"

# ─── Timeouts ──────────────────────────────────────────────────────────────────
# Timeout cho mỗi request (giây) — tăng lên vì Veo generation có thể chậm
timeout = 300         # 5 phút
graceful_timeout = 60 # Thời gian chờ shutdown mềm
keepalive = 5

# ─── Logging ───────────────────────────────────────────────────────────────────
loglevel = "info"
accesslog = "/var/log/web/access.log"
errorlog  = "/var/log/web/error.log"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)sµs'

# ─── Process ───────────────────────────────────────────────────────────────────
pidfile    = "/tmp/web.pid"
daemon     = False   # systemd sẽ quản lý process, không cần daemon

# ─── App preloading ────────────────────────────────────────────────────────────
# preload_app = True để tiết kiệm RAM (fork sau khi import app)
preload_app = True

# ─── Security ──────────────────────────────────────────────────────────────────
limit_request_line        = 8190
limit_request_fields      = 200
limit_request_field_size  = 16380
