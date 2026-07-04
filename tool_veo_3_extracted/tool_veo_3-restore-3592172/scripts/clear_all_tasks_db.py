"""
Delete all task-related records from the MongoDB database.

Usage from project root:
    python scripts/clear_all_tasks_db.py

This clears task collections only:
    - video_tasks
    - image_tasks
    - user_job_results

It does NOT delete users, API keys, projects, accounts, settings, or proxies.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from web.store import store  # noqa: E402


def main() -> None:
    before_video = store._video_tasks.count_documents({})
    before_image = store._image_tasks.count_documents({})
    before_user_jobs = store._user_job_results.count_documents({})

    res_video = store._video_tasks.delete_many({})
    res_image = store._image_tasks.delete_many({})
    res_user_jobs = store._user_job_results.delete_many({})

    remaining_video = store._video_tasks.count_documents({})
    remaining_image = store._image_tasks.count_documents({})
    remaining_user_jobs = store._user_job_results.count_documents({})

    print("=== Clear all task DB records ===")
    print(f"before_video_tasks={before_video}")
    print(f"before_image_tasks={before_image}")
    print(f"before_user_job_results={before_user_jobs}")
    print(f"deleted_video_tasks={res_video.deleted_count}")
    print(f"deleted_image_tasks={res_image.deleted_count}")
    print(f"deleted_user_job_results={res_user_jobs.deleted_count}")
    print(f"remaining_video_tasks={remaining_video}")
    print(f"remaining_image_tasks={remaining_image}")
    print(f"remaining_user_job_results={remaining_user_jobs}")


if __name__ == "__main__":
    main()
