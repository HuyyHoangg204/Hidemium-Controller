"""
remote_captcha.py — Lấy captcha token từ Remote Worker Server (C# API).

Flow giống hệt TokenProvider.cs:
  1. POST /Job/create-job  { SiteKey, Action }  → nhận jobId
  2. GET  /Job/check-job/{jobId}                → poll cho tới COMPLETED
  3. POST /Job/report-error { JobId }           → báo lỗi (optional)

Server URL mặc định: http://45.117.179.7:5858
"""

import logging
import time
import requests
from typing import Optional

logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────
DEFAULT_SERVER_URL = "http://45.117.179.7:5858"
DEFAULT_SITE_KEY = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"

POLL_INTERVAL = 2        # giây giữa mỗi lần check-job
JOB_TIMEOUT = 60         # 1 phút timeout tổng cho 1 job (giảm từ 180s để tránh treo cả flow)
MAX_RETRIES = 2           # tạo lại job tối đa 2 lần (giảm từ 3 — fail fast)
HTTP_TIMEOUT = 10         # timeout cho mỗi HTTP request

_server_url = DEFAULT_SERVER_URL


def set_server_url(url: str):
    """Thay đổi server URL runtime (gọi từ settings UI nếu cần)."""
    global _server_url
    _server_url = url.rstrip("/") if url else DEFAULT_SERVER_URL
    logger.info(f"[RemoteCaptcha] Server URL set to: {_server_url}")


def get_server_url() -> str:
    return _server_url


def request_remote_captcha(
    action: str = "IMAGE_GENERATION",
    site_key: str = DEFAULT_SITE_KEY,
    server_url: str = None,
    timeout: int = JOB_TIMEOUT,
) -> Optional[str]:
    """
    Yêu cầu giải captcha từ Remote Worker Server.
    
    Blocking call — tạo job, poll cho tới khi COMPLETED hoặc timeout.
    Tự động retry tạo job mới nếu status = FAILED/ERROR/TIMEOUT.

    Returns:
        token string hoặc None nếu timeout/lỗi
    """
    url = (server_url or _server_url).rstrip("/")
    
    for retry in range(1, MAX_RETRIES + 1):
        try:
            # ── 1. Create Job ──
            create_payload = {
                "SiteKey": site_key,
                "Action": action,
            }
            
            logger.info(
                f"[RemoteCaptcha] Creating job (retry={retry}/{MAX_RETRIES}) "
                f"action={action} → {url}/Job/create-job"
            )
            
            resp = requests.post(
                f"{url}/Job/create-job",
                json=create_payload,
                timeout=HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            
            data = resp.json()
            job_id = data.get("jobId") or data.get("JobId") or data.get("job_id")
            
            if not job_id:
                logger.error(f"[RemoteCaptcha] create-job response thiếu jobId: {data}")
                continue
            
            logger.info(f"[RemoteCaptcha] Job created: {job_id}")
            
            # ── 2. Poll for completion ──
            start_time = time.time()
            
            while time.time() - start_time < timeout:
                time.sleep(POLL_INTERVAL)
                
                try:
                    check_resp = requests.get(
                        f"{url}/Job/check-job/{job_id}",
                        timeout=HTTP_TIMEOUT,
                    )
                    
                    if not check_resp.ok:
                        continue
                    
                    job_data = check_resp.json()
                    status = (
                        job_data.get("status") 
                        or job_data.get("Status") 
                        or ""
                    ).upper()
                    
                    if status == "COMPLETED":
                        token = (
                            job_data.get("token") 
                            or job_data.get("Token") 
                            or job_data.get("result")
                        )
                        if token:
                            logger.info(
                                f"[RemoteCaptcha] ✅ Job {job_id} COMPLETED — "
                                f"token len={len(token)} "
                                f"(took {int(time.time() - start_time)}s)"
                            )
                            return token
                        else:
                            logger.warning(
                                f"[RemoteCaptcha] Job {job_id} COMPLETED nhưng token rỗng"
                            )
                            break  # tạo job mới
                    
                    elif status in ("FAILED", "ERROR", "TIMEOUT"):
                        logger.warning(
                            f"[RemoteCaptcha] Job {job_id} status={status} → tạo job mới"
                        )
                        # Report error
                        _report_error(url, job_id)
                        break  # tạo job mới
                    
                    # PENDING / PROCESSING / SOLVING → tiếp tục poll
                    
                except requests.RequestException:
                    # Lỗi mạng tạm thời khi poll → bỏ qua, poll lại
                    pass
            
            # Timeout → tạo job mới
            if time.time() - start_time >= timeout:
                logger.warning(
                    f"[RemoteCaptcha] Job {job_id} poll TIMEOUT ({timeout}s) → retry"
                )
                _report_error(url, job_id)
            
            time.sleep(1)  # nghỉ 1s trước khi tạo job mới
            
        except requests.RequestException as e:
            logger.error(f"[RemoteCaptcha] HTTP error (retry={retry}): {e}")
            time.sleep(2)
        except Exception as e:
            logger.error(f"[RemoteCaptcha] Unexpected error (retry={retry}): {e}")
            time.sleep(2)
    
    logger.error(f"[RemoteCaptcha] ❌ Thất bại sau {MAX_RETRIES} lần retry")
    return None


def _report_error(server_url: str, job_id: str):
    """Báo lỗi cho server (best-effort, không throw)."""
    try:
        requests.post(
            f"{server_url}/Job/report-error",
            json={"JobId": job_id},
            timeout=5,
        )
    except Exception:
        pass


def is_server_available(server_url: str = None) -> bool:
    """Quick check xem server có đang chạy không (3s timeout)."""
    url = (server_url or _server_url).rstrip("/")
    try:
        resp = requests.get(f"{url}/Job/check-job/test", timeout=3)
        # Bất kỳ response nào (kể cả 404) = server đang chạy
        return True
    except Exception:
        return False
