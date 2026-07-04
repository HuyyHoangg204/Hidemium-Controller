from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict

from web.exceptions import VietAutoAPIError
from web.store import store


logger = logging.getLogger(__name__)
_session_locks_guard = threading.RLock()
_session_locks: dict[str, threading.RLock] = {}


class AccountSessionError(VietAutoAPIError):
    """Small typed error for account-session failures."""

    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message, status_code)
        self.code = code

    def to_dict(self) -> dict:
        data = super().to_dict()
        data["code"] = self.code
        return data


class AccountSessionAPI:
    """Issue token + project_id for the Veo account assigned to a user."""

    def issue_for_account_name(self, account_name: str, *, user=None, force_refresh: bool = False) -> Dict[str, Any]:
        """Issue token + project_id for an explicit Gmail/Veo account."""
        account_name = (account_name or "").strip()
        if not account_name:
            raise AccountSessionError("INVALID_ACCOUNT", "Missing account name", 400)
        account = store.get_veo_account_by_name(account_name)
        if not account or not getattr(account, "is_active", False):
            raise AccountSessionError(
                "ACCOUNT_UNAVAILABLE",
                f"Veo account is unavailable: {account_name}",
                409,
            )
        with self._account_session_lock(account_name):
            return self._issue_for_account(account, user=user, force_refresh=force_refresh)

    def issue_for_user(self, user, *, force_refresh: bool = False) -> Dict[str, Any]:
        if not user or not getattr(user, "id", None):
            raise AccountSessionError("INVALID_API_KEY", "Invalid API key", 401)
        if not getattr(user, "is_active", False):
            raise AccountSessionError("INVALID_API_KEY", "Inactive user", 403)

        candidates = [
            a for a in store.list_veo_accounts_for_user(user.id, active_only=True)
            if getattr(a, "is_active", False)
        ]
        if not candidates:
            auto_account = store.get_or_assign_veo_account_for_user(user.id)
            if auto_account and getattr(auto_account, "is_active", False):
                candidates = [auto_account]
        if not candidates:
            # Last-resort compatibility for background/reference jobs: do not fail
            # just because the API user has no explicit assignment yet. Reuse any
            # active account from the shared pool. Some accounts can still issue
            # from cached api_session even when cookie is currently empty.
            candidates = [
                a for a in store.list_veo_accounts()
                if getattr(a, "is_active", False)
            ]
        candidates.sort(key=lambda a: (
            0 if not (isinstance(getattr(a, "api_session", None), dict) and getattr(a, "api_session", {}).get("issued_at")) else 1,
            getattr(a, "name", ""),
        ))
        if not candidates:
            raise AccountSessionError(
                "NO_ASSIGNED_ACCOUNT",
                "No active Veo account is assigned to this user",
                409,
            )

        items = []
        errors = []
        for account in candidates:
            try:
                with self._account_session_lock(getattr(account, "name", "")):
                    data = self._issue_for_account(account, user=user, force_refresh=force_refresh)
                items.append(data)
            except AccountSessionError as exc:
                errors.append({
                    "account_name": getattr(account, "name", ""),
                    "code": getattr(exc, "code", ""),
                    "error": str(exc),
                })
                logger.warning(
                    "[AccountSession] candidate failed user=%s account=%s code=%s error=%s",
                    getattr(user, "username", ""),
                    getattr(account, "name", ""),
                    getattr(exc, "code", ""),
                    exc,
                )
                continue

        if not items:
            raise AccountSessionError(
                "NO_VALID_ASSIGNED_ACCOUNT",
                "No assigned Veo account can issue token/project",
                409,
            )

        return {
            "items": items,
            "total": len(candidates),
            "success": len(items),
            "failed": len(errors),
            "errors": errors,
        }

    def _issue_for_account(self, account, *, user=None, force_refresh: bool = False) -> Dict[str, Any]:
        cookie = (getattr(account, "cookie", None) or "").strip()
        if not cookie:
            raise AccountSessionError(
                "SESSION_UNAVAILABLE",
                "Assigned Veo account has no cookie",
                409,
            )

        token = self._resolve_access_token(cookie, force_refresh=force_refresh)
        if not token:
            raise AccountSessionError(
                "SESSION_UNAVAILABLE",
                "Unable to resolve access token for assigned Veo account",
                502,
            )

        credit_info = self._verify_access_token_credit(token)
        if not credit_info.get("ok"):
            raise AccountSessionError(
                "INVALID_ACCESS_TOKEN",
                f"Access token failed credit check: {credit_info.get('error') or 'unknown error'}",
                502,
            )

        token_sha8 = hashlib.sha1(token.encode("utf-8", errors="ignore")).hexdigest()[:8]
        project_id, project_source = self._resolve_project_id(account, cookie, token, force_refresh=force_refresh)
        if not project_id and not force_refresh:
            logger.warning(
                "[AccountSession] project unresolved; retrying with fresh token account=%s token=%s",
                getattr(account, "name", ""),
                token_sha8,
            )
            fresh_token = self._resolve_access_token(cookie, force_refresh=True)
            if fresh_token:
                token = fresh_token
                token_sha8 = hashlib.sha1(token.encode("utf-8", errors="ignore")).hexdigest()[:8]
                project_id, project_source = self._resolve_project_id(account, cookie, token, force_refresh=True)
        if not project_id:
            logger.error(
                "[AccountSession] project resolve failed account=%s token=%s source=%s force_refresh=%s",
                getattr(account, "name", ""),
                token_sha8,
                project_source,
                force_refresh,
            )
            raise AccountSessionError(
                "SESSION_UNAVAILABLE",
                "Unable to resolve project id for assigned Veo account",
                502,
            )

        session_meta = {
            "issued_at": time.time(),
            "issued_to_user_id": getattr(user, "id", "") if user else "",
            "issued_to_username": getattr(user, "username", "") if user else "",
            "project_id": project_id,
            "token_sha256": hashlib.sha256(token.encode("utf-8", errors="ignore")).hexdigest(),
        }
        try:
            store.update_veo_account_api_session(account.id, session_meta)
        except Exception:
            # Metadata write must not break the successful token/project response.
            pass

        logger.info(
            "[AccountSession] account=%s user=%s token=%s project=%s source=%s force_refresh=%s",
            getattr(account, "name", ""),
            getattr(user, "username", ""),
            token_sha8,
            project_id,
            project_source,
            force_refresh,
        )

        return {
            "token": token,
            "project_id": project_id,
            "account_name": getattr(account, "name", ""),
        }

    def _account_session_lock(self, account_name: str):
        account_name = (account_name or "").strip()
        with _session_locks_guard:
            lock = _session_locks.get(account_name)
            if lock is None:
                lock = threading.RLock()
                _session_locks[account_name] = lock
            return lock

    def _resolve_access_token(self, cookie: str, *, force_refresh: bool = False) -> str | None:
        from core.veo_client import VeoClient

        client = VeoClient(cookie=cookie, proxy=None)
        token = None
        if not force_refresh:
            token = getattr(client, "access_token", None)
        if force_refresh or not token:
            try:
                client.get_session_token(warm_flow=True)
                token = getattr(client, "access_token", None)
            except Exception:
                token = None
        return token

    def _verify_access_token_credit(self, token: str, *, timeout: int = 20) -> dict:
        token = str(token or "").strip()
        result = {"ok": False, "error": ""}
        if not token:
            result["error"] = "empty token"
            return result
        try:
            request_obj = urllib.request.Request(
                "https://aisandbox-pa.googleapis.com/v1/credits?key=AIzaSyBtrm0o5ab1c-Ec8ZuLcGt3oJAA5VWt3pY",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "*/*",
                    "Origin": "https://labs.google",
                    "Referer": "https://labs.google/",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
                    "X-Browser-Channel": "stable",
                    "X-Browser-Year": "2026",
                },
                method="GET",
            )
            with urllib.request.urlopen(request_obj, timeout=timeout) as response:
                status = getattr(response, "status", None) or response.getcode()
                body = response.read().decode("utf-8", errors="replace")
            if int(status) != 200:
                result["error"] = f"HTTP {status}: {body[:300]}"
                return result
            data = json.loads(body or "{}")
            result.update({
                "ok": True,
                "credits": data.get("credits", 0),
                "tier": data.get("userPaygateTier") or data.get("serviceTier") or "UNKNOWN",
            })
            return result
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            result["error"] = f"HTTP {getattr(exc, 'code', '')}: {body[:300] or exc}"
            return result
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            return result

    def _resolve_project_id(self, account, cookie: str, token: str, *, force_refresh: bool = False) -> tuple[str | None, str]:
        cached = None
        api_session = getattr(account, "api_session", None)
        if isinstance(api_session, dict):
            cached = (api_session.get("project_id") or "").strip() or None
        logger.info(
            "[AccountSession] api_session account=%s has_project=%s project=%s force_refresh=%s",
            getattr(account, "name", ""),
            bool(cached),
            cached or "",
            force_refresh,
        )
        if cached:
            return cached, "api_session_cached"

        from core.project import create_project, search_user_projects

        projects = []
        project_source = "not_attempted"
        try:
            projects = search_user_projects(
                cookie=cookie,
                access_token=token,
                page_size=20,
                timeout=10,
                proxy=None,
            ) or []
            logger.info(
                "[AccountSession] project search account=%s count=%s cached=%s force_refresh=%s",
                getattr(account, "name", ""),
                len(projects),
                cached or "",
                force_refresh,
            )
            project_source = "search_empty" if not projects else "search"
        except Exception as exc:
            logger.warning(
                "[AccountSession] project search failed account=%s cached=%s force_refresh=%s error=%s",
                getattr(account, "name", ""),
                cached or "",
                force_refresh,
                exc,
            )
            project_source = "search_exception"

        for project in projects:
            project = project or {}
            existing = (project.get("projectId") or project.get("id") or "").strip()
            if cached and existing == cached:
                return cached, "validated_cached"

        if cached and projects:
            logger.warning(
                "[AccountSession] cached project mismatch account=%s cached=%s token_projects=%s",
                getattr(account, "name", ""),
                cached,
                [((p or {}).get("projectId") or (p or {}).get("id")) for p in projects[:5]],
            )

        if projects:
            first = projects[0] or {}
            existing = (first.get("projectId") or first.get("id") or "").strip()
            if existing:
                return existing, "search"

        if cached and not force_refresh:
            logger.warning(
                "[AccountSession] using unvalidated cached project account=%s cached=%s reason=project_search_empty",
                getattr(account, "name", ""),
                cached,
            )
            return cached, "unvalidated_cached"

        title = f"API Session - {getattr(account, 'name', 'Veo Account')}"
        try:
            created = create_project(
                project_title=title,
                tool_name="PINHOLE",
                cookie=cookie,
                access_token=token,
                timeout=10,
                proxy=None,
            )
            logger.info(
                "[AccountSession] project create account=%s result=%s force_refresh=%s",
                getattr(account, "name", ""),
                created or "",
                force_refresh,
            )
            return created, "created" if created else "create_empty"
        except Exception as exc:
            logger.warning(
                "[AccountSession] project create failed account=%s error=%s",
                getattr(account, "name", ""),
                exc,
            )
            return None, f"{project_source}|create_exception"


account_session_api = AccountSessionAPI()
