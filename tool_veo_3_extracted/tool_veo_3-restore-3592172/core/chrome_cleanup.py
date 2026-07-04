"""
Safe Chrome cleanup for tool-owned browser processes.

Option A policy:
- Never kill all chrome.exe globally.
- Only terminate Chrome/Edge processes whose command line contains a registered
  tool profile directory or a registered remote debugging port marker.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_TOOL_PROFILES: dict[str, dict] = {}
_DEBUG_PORTS: dict[str, dict] = {}
_PROJECT_PROFILES: dict[str, set[str]] = {}
_LAST_CLEANUP: dict[str, float] = {}

ROOT_DIR = Path(__file__).resolve().parents[1]
SAFE_PROFILE_DIR_NAMES = {
    "chrome_profiles",
    "browser_profiles",
    "playwright_profiles",
    "captcha_profiles",
}


def _norm_path(path: str | os.PathLike | None) -> str:
    if not path:
        return ""
    try:
        return str(Path(path).resolve()).lower().replace("/", "\\")
    except Exception:
        return str(path).lower().replace("/", "\\")


def _is_safe_tool_profile(path: str) -> bool:
    norm = _norm_path(path)
    if not norm:
        return False
    root = _norm_path(ROOT_DIR)
    if not norm.startswith(root):
        return False
    parts = set(Path(norm).parts)
    return bool(parts.intersection(SAFE_PROFILE_DIR_NAMES))


def register_tool_profile(profile_dir: str, owner: str | None = None, project_id: str | None = None) -> bool:
    """Register a browser profile as tool-owned if it is inside a safe tool dir."""
    norm = _norm_path(profile_dir)
    if not _is_safe_tool_profile(norm):
        logger.warning("[ChromeCleanup] ignore unsafe profile registration: %s", profile_dir)
        return False
    with _LOCK:
        _TOOL_PROFILES[norm] = {
            "owner": owner or "",
            "project_id": project_id or "",
            "registered_at": time.time(),
        }
        if project_id:
            _PROJECT_PROFILES.setdefault(project_id, set()).add(norm)
    return True


def register_debug_port(port: int | str, owner: str | None = None, project_id: str | None = None) -> bool:
    port_s = str(port or "").strip()
    if not port_s.isdigit():
        return False
    with _LOCK:
        _DEBUG_PORTS[port_s] = {
            "owner": owner or "",
            "project_id": project_id or "",
            "registered_at": time.time(),
        }
    return True


def _query_browser_processes() -> list[dict]:
    """Return browser processes with pid/name/command_line on Windows."""
    ps = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -match '^(chrome|msedge)\\.exe$' } | "
        "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        cp = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=8,
            encoding="utf-8",
            errors="replace",
        )
        if cp.returncode != 0 or not cp.stdout.strip():
            return []
        data = json.loads(cp.stdout)
        if isinstance(data, dict):
            data = [data]
        result = []
        for row in data or []:
            result.append({
                "pid": int(row.get("ProcessId") or 0),
                "name": row.get("Name") or "",
                "cmd": row.get("CommandLine") or "",
            })
        return [r for r in result if r["pid"] and r["cmd"]]
    except Exception as e:
        logger.warning("[ChromeCleanup] process query failed: %s", e)
        return []


def _matches_registered_marker(cmd: str, profiles: Iterable[str], ports: Iterable[str]) -> tuple[bool, str]:
    cmd_norm = _norm_path(cmd)
    for profile in profiles:
        p = _norm_path(profile)
        if p and p in cmd_norm:
            return True, f"profile={Path(profile).name}"
    for port in ports:
        if f"--remote-debugging-port={port}" in cmd or f"--remote-debugging-port {port}" in cmd:
            return True, f"debug_port={port}"
    return False, ""


def cleanup_tool_chrome(
    *,
    project_id: str | None = None,
    owner: str | None = None,
    reason: str = "manual",
    min_interval_sec: float = 10.0,
) -> dict:
    """Safely terminate only registered tool-owned Chrome/Edge processes."""
    key = project_id or owner or "global"
    now = time.time()
    with _LOCK:
        last = _LAST_CLEANUP.get(key, 0)
        if min_interval_sec and now - last < min_interval_sec:
            return {"skipped": True, "reason": "rate_limited", "killed": 0}
        _LAST_CLEANUP[key] = now

        profiles = set(_TOOL_PROFILES.keys())
        ports = set(_DEBUG_PORTS.keys())
        if project_id:
            profiles = set(_PROJECT_PROFILES.get(project_id, set())) or profiles
            ports = {p for p, meta in _DEBUG_PORTS.items() if meta.get("project_id") == project_id} or ports
        if owner:
            profiles = {p for p, meta in _TOOL_PROFILES.items() if meta.get("owner") == owner} or profiles
            ports = {p for p, meta in _DEBUG_PORTS.items() if meta.get("owner") == owner} or ports

    killed = []
    for proc in _query_browser_processes():
        matched, marker = _matches_registered_marker(proc["cmd"], profiles, ports)
        if not matched:
            continue
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc["pid"]), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=8,
            )
            killed.append({"pid": proc["pid"], "name": proc["name"], "marker": marker})
            logger.info(
                "[ChromeCleanup] killed pid=%s name=%s marker=%s reason=%s project=%s owner=%s",
                proc["pid"], proc["name"], marker, reason, project_id or "", owner or "",
            )
        except Exception as e:
            logger.warning("[ChromeCleanup] failed pid=%s marker=%s: %s", proc["pid"], marker, e)

    logger.info(
        "[ChromeCleanup] done reason=%s project=%s owner=%s killed=%d",
        reason, project_id or "", owner or "", len(killed),
    )
    return {"skipped": False, "killed": len(killed), "processes": killed}


def cleanup_project(project_id: str, reason: str = "project_completed") -> dict:
    return cleanup_tool_chrome(project_id=project_id, reason=reason)


def cleanup_owner(owner: str, reason: str = "owner_cleanup") -> dict:
    return cleanup_tool_chrome(owner=owner, reason=reason)


def get_registered_state() -> dict:
    with _LOCK:
        return {
            "profiles": list(_TOOL_PROFILES.keys()),
            "debug_ports": list(_DEBUG_PORTS.keys()),
            "project_profiles": {k: sorted(v) for k, v in _PROJECT_PROFILES.items()},
        }
