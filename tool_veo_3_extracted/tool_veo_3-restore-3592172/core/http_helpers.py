import time
import requests
from typing import Optional, Dict, Any


class HTTPError(Exception):
    pass


def request_with_retries(
    method: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Any] = None,
    data: Optional[Any] = None,
    proxies: Optional[Dict[str, str]] = None,
    timeout: int = 10,
    retries: int = 3,
    backoff: float = 1.0,
):
    """Simple requests wrapper with retries and exponential backoff.

    Returns Response object on success or raises HTTPError on failure.
    """
    method = method.lower()
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                data=data,
                proxies=proxies,
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as e:
            last_exc = e
            if attempt == retries:
                break
            sleep_for = backoff * (2 ** (attempt - 1))
            time.sleep(sleep_for)
    raise HTTPError(f"Failed request {method.upper()} {url}: {last_exc}")
