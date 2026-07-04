from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import requests


class HidemiumError(RuntimeError):
    """Raised when the Hidemium API returns an error or cannot be reached."""


class HidemiumClient:
    """Small client for Hidemium Automation API V4 running on localhost:2222."""

    def __init__(self, base_url: str = "http://127.0.0.1:2222", timeout: int = 30, logger: logging.Logger | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.logger = logger or logging.getLogger("hidemium_controller.api")
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json, text/plain, */*"})
        self._token: Optional[str] = None

    def _get_token(self) -> Optional[str]:
        """Fetch JWT token from Hidemium local API and cache it."""
        if self._token:
            return self._token
        try:
            resp = self.session.get(f"{self.base_url}/user-settings/token", timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                self._token = data.get("token")
                self.logger.debug("Token fetched OK")
        except Exception:  # noqa: BLE001
            pass
        return self._token

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        started = time.perf_counter()
        body = kwargs.get("json") or kwargs.get("data")
        self.logger.info("API %s %s", method.upper(), url)
        if body is not None:
            self.logger.debug("API request body: %s", json.dumps(body, ensure_ascii=False, default=str))

        # Inject Authorization header automatically
        token = self._get_token()
        headers = kwargs.pop("headers", {})
        if token:
            headers["Authorization"] = token
        if headers:
            kwargs["headers"] = headers

        try:
            response = self.session.request(method, url, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            elapsed = (time.perf_counter() - started) * 1000
            self.logger.exception("API failed %s %s after %.0fms: %s", method.upper(), url, elapsed, exc)
            raise HidemiumError(f"Không kết nối được Hidemium API tại {url}: {exc}") from exc

        elapsed = (time.perf_counter() - started) * 1000
        text = response.text.strip()
        self.logger.info("API response %s %s -> HTTP %s in %.0fms", method.upper(), url, response.status_code, elapsed)
        self.logger.debug("API response body: %s", text[:5000])
        if response.status_code >= 400:
            raise HidemiumError(f"HTTP {response.status_code}: {text[:500]}")

        if not text:
            return {"ok": True, "status_code": response.status_code}
        try:
            return response.json()
        except ValueError:
            return text

    @staticmethod
    def _bool(value: bool) -> str:
        return "true" if value else "false"

    def check_authorize(self, uuid: str) -> Any:
        return self._request("GET", f"/authorize?{urlencode({'uuid': uuid})}")

    def get_user_uuid(self) -> Any:
        return self._request("GET", "/user-settings/token")

    def list_profiles(self, is_local: bool = False, page: int = 1, limit: int = 50, search: str = "", folder_ids: list | None = None) -> Any:
        params = {"is_local": self._bool(is_local)}
        payload = {
            "page": page,
            "limit": limit,
            "search": search,
            "orderName": 0,
            "orderLastOpen": 0,
            "date_range": ["", ""],
            "folder_id": folder_ids if folder_ids else [],
            "status": "",
        }
        return self._request("POST", f"/v1/browser/list?{urlencode(params)}", json=payload)

    def get_profile(self, uuid: str, is_local: bool = False) -> Any:
        return self._request("GET", f"/v2/browser/get-profile-by-uuid/{uuid}?{urlencode({'is_local': self._bool(is_local)})}")

    def open_profile(
        self,
        uuid: str,
        command: str = "",
        proxy: str = "",
    ) -> Any:
        params: Dict[str, str] = {"uuid": uuid}
        if command:
            params["command"] = command
        if proxy:
            params["proxy"] = proxy
        return self._request("GET", f"/openProfile?{urlencode(params)}")

    def close_profile(self, uuid: str) -> Any:
        return self._request("GET", f"/closeProfile?{urlencode({'uuid': uuid})}")

    def create_profile_by_default(self, payload: Dict[str, Any], is_local: bool = False) -> Any:
        return self._request("POST", f"/create-profile-by-default?{urlencode({'is_local': self._bool(is_local)})}", json=payload)

    def create_profile_custom(self, payload: Dict[str, Any], is_local: bool = True) -> Any:
        return self._request("POST", f"/create-profile-custom?{urlencode({'is_local': self._bool(is_local)})}", json=payload)

    def delete_profile(self, ids_or_uuids: list[Any], is_local: bool = False) -> Any:
        return self._request("DELETE", f"/v1/browser/destroy?{urlencode({'is_local': self._bool(is_local)})}", json={"uuid_browser": ids_or_uuids})

    def edit_proxy(self, browser_uuid: str, proxy_id: int = -1, is_local: bool = True, **extra: Any) -> Any:
        payload = {"browser_uuid": browser_uuid, "id": proxy_id, **extra}
        return self._request("PUT", f"/v2/proxy/quick-edit?{urlencode({'is_local': self._bool(is_local)})}", json=payload)

    def list_status(self, is_local: bool = True) -> Any:
        return self._request("GET", f"/v2/status-profile?{urlencode({'is_local': self._bool(is_local)})}")

    def list_tags(self, is_local: bool = True) -> Any:
        return self._request("GET", f"/v2/tag?{urlencode({'is_local': self._bool(is_local)})}")

    def list_default_configs(self, page: int = 1, limit: int = 10) -> Any:
        return self._request("GET", f"/v2/default-config?{urlencode({'page': page, 'limit': limit})}")

    def list_versions(self) -> Any:
        return self._request("GET", "/v2/browser/get-list-version")

    def list_folders(self, is_local: bool = False, page: int = 1, limit: int = 50) -> Any:
        return self._request("GET", f"/v1/folder/list?{urlencode({'is_local': self._bool(is_local), 'page': page, 'limit': limit})}")

    def update_profile_name(self, profile_uuid: str, name: str, is_local: bool = False) -> Any:
        payload = {"column": "name", "profile_uuid": profile_uuid, "data": name}
        return self._request("PUT", f"/v2/browser/update-once?{urlencode({'is_local': self._bool(is_local)})}", json=payload)

    def update_profile_note(self, profile_uuid: str, note: str, is_local: bool = False) -> Any:
        payload = {"profile_uuid": profile_uuid, "note": note}
        return self._request("PUT", f"/v2/browser/update-note?{urlencode({'is_local': self._bool(is_local)})}", json=payload)

    def change_profile_status(self, browser_uuid: str, status_id: int, is_local: bool = True) -> Any:
        payload = {"browser_uuid": browser_uuid, "id": status_id}
        return self._request("PUT", f"/v2/status-profile/change-status?{urlencode({'is_local': self._bool(is_local)})}", json=payload)

    def sync_tags(self, profile_uuid: str, tags: list[str], is_local: bool = True) -> Any:
        payload = {"profile_uuid": profile_uuid, "tags": tags}
        return self._request("POST", f"/v2/tag?{urlencode({'is_local': self._bool(is_local)})}", json=payload)

    def add_profile_to_folder(self, folder_uuid: str, profile_uuids: list[str], is_local: bool = True) -> Any:
        payload = {"uuid_browser": profile_uuids}
        return self._request("POST", f"/v1/folder/{folder_uuid}/add-browser?{urlencode({'is_local': self._bool(is_local)})}", json=payload)

    def change_fingerprint(self, profile_uuid: str, is_local: bool = False) -> Any:
        return self._request("PUT", f"/v2/browser/change-fingerprint?{urlencode({'is_local': self._bool(is_local)})}", json={"profile_uuid": profile_uuid})

    def update_profile_proxy(self, updates: list[dict[str, str]], is_local: bool = False) -> Any:
        return self._request("POST", f"/v2/browser/proxy/update?{urlencode({'is_local': self._bool(is_local)})}", json={"browser_update": updates})

    def list_scripts(self, page: int = 1, limit: int = 20) -> Any:
        return self._request("GET", f"/v2/automation/script?{urlencode({'page': page, 'limit': limit})}")

    def list_campaigns(self, page: int = 1, limit: int = 20, search: str = "") -> Any:
        return self._request("GET", f"/automation/campaign?{urlencode({'search': search, 'page': page, 'limit': limit})}")

    def create_campaign(self, payload: Dict[str, Any]) -> Any:
        return self._request("POST", "/automation/campaign", json=payload)

    def delete_campaign(self, campaign_ids: list[int]) -> Any:
        return self._request("POST", "/automation/delete-campaign", json={"ids": campaign_ids})

    def add_profiles_to_campaign(self, payload: Dict[str, Any]) -> Any:
        return self._request("POST", "/automation/campaign/save-campaign-profile", json=payload)

    def update_campaign_input_variables(self, payload: Dict[str, Any]) -> Any:
        return self._request("POST", "/automation/campaign/save-auto-campaign", json=payload)

    def set_campaign_variables(self, campaign_id: int, variables: list[dict[str, Any]]) -> Any:
        return self._request("POST", "/automation/campaign/update-variables", json={"campaign_id": campaign_id, "variables": variables})

    def delete_all_profiles_in_campaign(self, campaign_id: int) -> Any:
        return self._request("DELETE", "/automation/campaign/delete-all-campaign-profile", json={"campaignId": str(campaign_id)})

    def list_schedules(self, campaign_id: int, page: int = 1, limit: int = 20) -> Any:
        return self._request("GET", f"/automation/schedule?{urlencode({'campaign_id': campaign_id, 'page': page, 'limit': limit})}")

    def create_schedule(self, payload: Dict[str, Any]) -> Any:
        return self._request("POST", "/automation/schedule", json=payload)

    def update_schedule_status(self, payload: Dict[str, Any]) -> Any:
        return self._request("PUT", "/automation/update-schedule-status", json=payload)

    def delete_schedule(self, schedule_ids: list[int]) -> Any:
        return self._request("POST", "/automation/delete-schedule", json={"ids": schedule_ids})


def pretty(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) if not isinstance(data, str) else data
