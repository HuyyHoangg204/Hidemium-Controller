"""
web - Custom REST API server theo pattern VietAuto.AI
Hoàn toàn độc lập với logic chính của project.

Usage:
    python -m web.server --port 8080 --api-key my_secret_key
    # OR
    python web/run.py
"""

from web.models import (
    VeoModel,
    ScreenRatio,
    TaskStatus,
    ActionType,
    Project,
    VideoTask,
    ImageTask,
)
from web.exceptions import (
    VietAutoAPIError,
    AuthenticationError,
    ValidationError,
    TaskNotFoundError,
    UpstreamAPIError,
    TaskTimeoutError,
)
from web.store import store
from web.veo_service import VeoService
from web.server import create_app

__version__ = "1.0.0"
__all__ = [
    "VeoModel",
    "ScreenRatio",
    "TaskStatus",
    "ActionType",
    "Project",
    "VideoTask",
    "ImageTask",
    "VietAutoAPIError",
    "AuthenticationError",
    "ValidationError",
    "TaskNotFoundError",
    "UpstreamAPIError",
    "TaskTimeoutError",
    "store",
    "VeoService",
    "create_app",
]
