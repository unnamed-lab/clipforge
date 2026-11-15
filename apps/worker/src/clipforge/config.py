from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    candidate = Path(raw.strip())
    if not candidate.is_absolute():
        candidate = (REPO_ROOT / candidate).resolve()
    return candidate


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    host: str
    port: int
    token: str
    max_upload_bytes: int
    max_duration_ms: int
    min_free_bytes: int
    whisper_model: str
    whisper_device: str
    whisper_compute_type: str
    llm_enabled: bool
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    llm_timeout_seconds: int
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    def ensure_dirs(self) -> None:
        for directory in (
            self.data_dir,
            self.projects_dir,
            self.models_dir,
            self.logs_dir,
            self.tmp_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


def _load_or_create_token(data_dir: Path) -> str:
    env_token = os.environ.get("CLIPFORGE_WORKER_TOKEN", "").strip()
    if env_token:
        return env_token
    token_file = data_dir / "worker-token"
    if token_file.exists():
        value = token_file.read_text(encoding="utf-8").strip()
        if value:
            return value
    token = secrets.token_urlsafe(32)
    token_file.write_text(token, encoding="utf-8")
    try:  # best effort on POSIX; Windows relies on the parent directory ACL
        os.chmod(token_file, 0o600)
    except OSError:
        pass
    return token


def load_settings() -> Settings:
    data_dir = _env_path("CLIPFORGE_DATA_DIR", REPO_ROOT / "data")
    data_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        data_dir=data_dir,
        host=os.environ.get("CLIPFORGE_WORKER_HOST", "127.0.0.1"),
        port=_env_int("CLIPFORGE_WORKER_PORT", 8788),
        token=_load_or_create_token(data_dir),
        max_upload_bytes=_env_int("CLIPFORGE_MAX_UPLOAD_MB", 2048) * 1024 * 1024,
        max_duration_ms=_env_int("CLIPFORGE_MAX_DURATION_MINUTES", 360) * 60 * 1000,
        min_free_bytes=_env_int("CLIPFORGE_MIN_FREE_GB", 2) * 1024 * 1024 * 1024,
        whisper_model=os.environ.get("CLIPFORGE_WHISPER_MODEL", "small").strip() or "small",
        whisper_device=os.environ.get("CLIPFORGE_WHISPER_DEVICE", "cpu").strip() or "cpu",
        whisper_compute_type=os.environ.get("CLIPFORGE_WHISPER_COMPUTE_TYPE", "int8").strip() or "int8",
        llm_enabled=_env_bool("CLIPFORGE_LLM_ENABLED", False),
        llm_base_url=os.environ.get("CLIPFORGE_LLM_BASE_URL", "").strip(),
        llm_model=os.environ.get("CLIPFORGE_LLM_MODEL", "").strip(),
        llm_api_key=os.environ.get("CLIPFORGE_LLM_API_KEY", "").strip(),
        llm_timeout_seconds=_env_int("CLIPFORGE_LLM_TIMEOUT_SECONDS", 60),
    )
    return settings
