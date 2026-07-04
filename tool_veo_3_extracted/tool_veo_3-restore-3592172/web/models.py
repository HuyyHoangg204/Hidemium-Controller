"""
Dataclasses và Enums cho VietAuto-style API.
"""

from dataclasses import dataclass, field, asdict
from typing import Optional, List, Any, Dict
from enum import Enum
import uuid
import time


# ─────────────────────── ENUMS ───────────────────────


class VeoModel(str, Enum):
    """Các model Veo hỗ trợ (map sang internal model key)."""

    # Public profile aliases from the reference worker.
    FAST = "FAST"
    LOW_PRIORITY = "LOW_PRIORITY"
    # Backward-compatible API aliases.
    T2V_FAST = "VEO_3.1_FAST"
    T2V_FAST_LOW = "VEO_3.1_FAST_LOWER_PRIORITY"
    T2V_QUALITY = "VEO_3.1_QUALITY"
    I2V_FAST = "VEO_3.1_I2V_FAST"
    I2V_QUALITY = "VEO_3.1_I2V_QUALITY"


class ImageModel(str, Enum):
    """Các model tạo ảnh."""

    GEM_PIX_2 = "GEM_PIX_2"
    NARWHAL = "NARWHAL"


class ScreenRatio(str, Enum):
    LANDSCAPE = "16:9"
    PORTRAIT = "9:16"


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    PAUSED = "PAUSED"


class UserRole(str, Enum):
    ADMIN = "ADMIN"
    DEV = "DEV"
    TESTER = "TESTER"


class Permission(str, Enum):
    CREATE_VIDEO = "CREATE_VIDEO"
    SHARE_VIDEO = "SHARE_VIDEO"
    VIEW_VIDEO = "VIEW_VIDEO"


class ActionType(str, Enum):
    TEXT_TO_VIDEO = "TEXT_TO_VIDEO"
    IMAGE_TO_VIDEO = "IMAGE_TO_VIDEO"
    IMAGES_TO_VIDEO = "IMAGES_TO_VIDEO"
    FRAMES_TO_VIDEO = "FRAMES_TO_VIDEO"
    CREATE_IMAGE = "CREATE_IMAGE"


# ─────────────────────── MODEL KEY MAPPING ───────────────────────

# Map từ API-facing model/profile → internal model key của Google Flow.
# Đồng bộ với repo mẫu `tool_veo3_mau/video_model_config.py`.
_VEO_MODEL_MAP: Dict[str, Dict[str, str]] = {
    VeoModel.FAST: {
        "16:9": "veo_3_1_t2v_fast_ultra",
        "9:16": "veo_3_1_t2v_fast_ultra",
    },
    VeoModel.LOW_PRIORITY: {
        "16:9": "veo_3_1_t2v_lite_low_priority",
        "9:16": "veo_3_1_t2v_lite_low_priority",
    },
    VeoModel.T2V_FAST: {
        "16:9": "veo_3_1_t2v_fast_ultra",
        "9:16": "veo_3_1_t2v_fast_ultra",
    },
    VeoModel.T2V_FAST_LOW: {
        "16:9": "veo_3_1_t2v_lite_low_priority",
        "9:16": "veo_3_1_t2v_lite_low_priority",
    },
    VeoModel.T2V_QUALITY: {
        "16:9": "veo_3_1_t2v_fast_ultra",
        "9:16": "veo_3_1_t2v_fast_ultra",
    },
    VeoModel.I2V_FAST: {
        "16:9": "veo_3_1_i2v_s_fast_ultra",
        "9:16": "veo_3_1_i2v_s_fast_portrait_ultra",
    },
    VeoModel.I2V_QUALITY: {
        "16:9": "veo_3_1_i2v_s_fast_ultra",
        "9:16": "veo_3_1_i2v_s_fast_portrait_ultra",
    },
}


def resolve_model_key(model: str, screen_ratio: str) -> str:
    """Chuyển đổi model string + ratio → internal Google Flow model key."""
    ratio = screen_ratio if screen_ratio in ("16:9", "9:16") else "16:9"
    model_map = _VEO_MODEL_MAP.get(model, _VEO_MODEL_MAP[VeoModel.FAST])
    return model_map.get(ratio, model_map["16:9"])


# ─────────────────────── DATACLASSES ───────────────────────


@dataclass
class Project:
    id: str
    name: str
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    video_count: int = 0
    total_videos: int = 0
    # Auto-set bởi project_tracker khi project hoàn tất task cuối cùng.
    # started_at = min(task.created_at), finished_at = max(task.completed_at).
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class VideoTask:
    id: str
    project_id: str
    name: str
    action_type: str
    model: str
    screen_ratio: str
    prompts: List[str]
    status: str = TaskStatus.PENDING
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # Internal tracking
    op_name: Optional[str] = None  # Google Labs operation name
    scene_id: Optional[str] = None  # Scene ID
    media_id: Optional[str] = None  # Result media ID / URL
    output_filename: Optional[str] = None  # Tên file video đã lưu (basename)
    error: Optional[str] = None
    veo_cookie: Optional[str] = None  # User's specific Google Cookie
    proxy_url: Optional[str] = None   # User's specific Proxy Server
    image_refs: Optional[List[str]] = None  # Images for NARWHAL generation
    count: int = 1  # Số ảnh/video tạo mỗi prompt
    raw_result: Optional[Any] = None
    completed_at: Optional[float] = None  # Unix timestamp khi task COMPLETED
    picked_account_name: Optional[str] = None  # Tên account trong pool được chọn để render
    # Pin task vào 1 account cụ thể — set bởi _distribute_and_preupload sau khi
    # account đó đã upload sẵn ảnh tham chiếu. Worker BẮT BUỘC chạy account này,
    # không swap (vì mediaId gắn cookie). Account chết → task FAIL.
    pinned_account_name: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        # raw_result được giữ lại để lưu vào MongoDB (cần cho retry I2V với images_b64)
        return d

    def to_public_dict(self) -> dict:
        """Response trả về cho client - chỉ bao gồm public fields."""
        raw = self.raw_result if isinstance(self.raw_result, dict) else {}
        return {
            "id": self.id,
            "project_id": self.project_id,
            "name": self.name,
            "action_type": self.action_type,
            "model": self.model,
            "screen_ratio": self.screen_ratio,
            "prompts": self.prompts,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "media_id": self.media_id,
            "output_filename": self.output_filename,
            "error": self.error,
            "image_refs": self.image_refs,
            "completed_at": self.completed_at,
            "picked_account_name": self.picked_account_name,
            "pinned_account_name": self.pinned_account_name,
            "source": raw.get("source", None),
            "image_paths": raw.get("image_paths", None),
            "raw_result": raw,
        }


@dataclass
class ImageTask:
    id: str
    project_id: str
    name: str
    model: str
    screen_ratio: str
    prompts: List[str]
    status: str = TaskStatus.PENDING
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    media_id: Optional[str] = None
    error: Optional[str] = None

    def to_public_dict(self) -> dict:
        return asdict(self)


@dataclass
class APIKey:
    key: str
    name: str
    created_at: float = field(default_factory=time.time)
    credits: int = 100
    is_active: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class User:
    id: str
    username: str
    api_key: str  # Dùng tương tự token sau khi đăng nhập
    password_hash: str  # Lưu mật khẩu đã mã hoá
    role: str
    permissions: List[str]
    created_at: float = field(default_factory=time.time)
    is_active: bool = True
    default_veo_cookie: Optional[str] = None
    default_proxy_url: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    def to_public_dict(self) -> dict:
        d = asdict(self)
        d.pop("password_hash", None)  # Không bao giờ trả ra password hash
        return d


@dataclass
class UserJobResult:
    """Ket qua job do user/client day ve qua API rieng."""

    id: str
    user_id: str
    project_name: str
    job_id: str
    prompt: str
    status: str
    api_key: Optional[str] = None
    download_url: Optional[str] = None
    duration_seconds: Optional[float] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_public_dict(self) -> dict:
        return asdict(self)


@dataclass
class VeoAccount:
    """Đại diện cho 1 tài khoản Veo (Cookie tĩnh + Proxy)."""
    id: str  # UID
    name: str  # Tên hiển thị (ví dụ: Account 1)
    cookie: str  # Chuỗi cookie từ trình duyệt
    proxy: Optional[str] = None         # Rotating proxy (captcha) — IP xoay mỗi request
    static_proxy: Optional[str] = None  # Static proxy (API calls) — IP cố định 1 account
    is_active: bool = True  # Trạng thái bật/tắt cả cụm (cookie + proxy)
    created_at: float = field(default_factory=time.time)
    # ID của User (Dev) được gán cookie này - None = RẢNH (chưa gán)
    assigned_to_user_id: Optional[str] = None
    # Grok cookies — auto-pushed từ Chrome Extension (list of {name, value, domain})
    grok_cookies: Optional[Any] = None
    # Lý do bị ban (lưu persist để UI hiển thị sau restart)
    ban_reason: Optional[str] = None
    # Metadata cho API /api/veo/account-session: project/token issued gần nhất
    api_session: Optional[Dict[str, Any]] = None

    def to_dict(self) -> dict:
        return asdict(self)
