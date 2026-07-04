from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
LEGACY_JOB_ARCHIVE_DIR = ROOT_DIR / "web" / "data" / "job_result_archive"
JOB_ARCHIVE_DIR = ROOT_DIR / "job_result_archive"
JOB_ARCHIVE_TXT_MAX_BYTES = 100 * 1024 * 1024  # 100MB/file, then rotate to job_results_002.txt



def next_job_archive_file(archive_dir: Path, incoming_bytes: int = 0) -> Path:
    """Return a daily TXT archive path, rotating when the current file is too large."""
    base_file = archive_dir / "job_results.txt"
    if not base_file.exists() or base_file.stat().st_size + incoming_bytes <= JOB_ARCHIVE_TXT_MAX_BYTES:
        return base_file
    index = 2
    while True:
        candidate = archive_dir / f"job_results_{index:03d}.txt"
        if not candidate.exists() or candidate.stat().st_size + incoming_bytes <= JOB_ARCHIVE_TXT_MAX_BYTES:
            return candidate
        index += 1


def migrate_legacy_job_archives_to_daily_txt(*, remove_legacy: bool = False) -> dict:
    """Gom file JSON cũ về một file TXT chung theo ngày, không cần import web.store/MongoDB."""
    summary = {
        "legacy_dir": str(LEGACY_JOB_ARCHIVE_DIR),
        "target_dir": str(JOB_ARCHIVE_DIR),
        "days": 0,
        "files_migrated": 0,
        "files_skipped": 0,
        "errors": [],
    }
    if not LEGACY_JOB_ARCHIVE_DIR.exists():
        return summary

    for day_dir in sorted(p for p in LEGACY_JOB_ARCHIVE_DIR.iterdir() if p.is_dir()):
        day = day_dir.name
        target_day_dir = JOB_ARCHIVE_DIR / day
        target_day_dir.mkdir(parents=True, exist_ok=True)
        target_file = target_day_dir / "job_results.txt"
        day_touched = False

        for legacy_file in sorted(day_dir.glob("*.json")):
            try:
                with legacy_file.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                archive_entry = {
                    "archive_record_id": uuid.uuid4().hex,
                    "archived_at": datetime.now().timestamp(),
                    "migrated_from": str(legacy_file),
                    "job_id": data.get("job_id") or data.get("id") or legacy_file.stem,
                    "data": data,
                }
                archive_line = json.dumps(archive_entry, ensure_ascii=False, default=str) + "\n"
                target_file = next_job_archive_file(target_day_dir, len(archive_line.encode("utf-8")))
                with target_file.open("a", encoding="utf-8") as f:
                    f.write(archive_line)
                summary["files_migrated"] += 1
                day_touched = True
                if remove_legacy:
                    legacy_file.unlink()
                if summary["files_migrated"] % 25 == 0:
                    print(f"Migrated {summary['files_migrated']} files...", flush=True)
            except Exception as exc:
                summary["files_skipped"] += 1
                summary["errors"].append({"file": str(legacy_file), "error": str(exc)})

        if day_touched:
            summary["days"] += 1
        if remove_legacy:
            try:
                day_dir.rmdir()
            except OSError:
                pass

    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gom legacy job archive JSON files into daily TXT files")
    parser.add_argument("--remove-legacy", action="store_true", help="Xoá file JSON cũ sau khi migrate thành công")
    args = parser.parse_args()
    result = migrate_legacy_job_archives_to_daily_txt(remove_legacy=args.remove_legacy)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
