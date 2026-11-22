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


def _env_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    values = tuple(value.strip() for value in raw.split(",") if value.strip())
    return values or default


DEFAULT_CORS_ORIGINS = ("http://localhost:3000", "http://127.0.0.1:3000")


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
    max_moments_per_request: int = 30
    youtube_api_key: str = ""
    youtube_suggest_enabled: bool = False
    trends_endpoint: str = ""
    trends_api_key: str = ""
    vidiq_enabled: bool = False
    twitch_client_id: str = ""
    twitch_client_secret: str = ""
    trend_cache_ttl_seconds: int = 21_600
    trend_stale_ttl_seconds: int = 604_800
    trend_timeout_seconds: int = 12
    cors_origins: tuple[str, ...] = DEFAULT_CORS_ORIGINS
    storage_backend: str = "local"
    supabase_url: str = ""
    supabase_service_key: str = ""
    supabase_bucket: str = "clipforge"
    supabase_storage_prefix: str = "/storage/v1"
    supabase_timeout_seconds: int = 60
    # -- autonomous production -------------------------------------------------
    # Every limit is configurable. The defaults are conservative safety
    # boundaries, not predictions: a stage timeout stops a dead process, a
    # stall is evidence of no heartbeat, and the deadline is the last resort.
    autoproduce_enabled: bool = True
    autoproduce_clip_limit: int = 8
    autoproduce_repair_limit: int = 2
    autoproduce_technical_retries: int = 1
    pipeline_stage_timeout_seconds: int = 1_800
    pipeline_deadline_seconds: int = 4 * 60 * 60
    pipeline_stall_seconds: int = 300
    pipeline_heartbeat_seconds: float = 10.0
    render_timeout_seconds: int = 2_400
    # Which provider the registry may use. Empty means "derive from the legacy
    # base URL": a configured endpoint is openai_compatible, otherwise none.
    llm_provider: str = ""
    llm_execution: str = "local"
    # Remote providers are opt-in. A loopback endpoint is local by definition.
    llm_allow_remote: bool = False
    llm_editorial_enabled: bool = False
    llm_editorial_model: str = ""
    # Where approved deliverables are copied. None means Documents/Clipforge.
    export_dir: Path | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def effective_export_dir(self) -> Path:
        root = self.export_dir or (Path.home() / "Documents" / "Clipforge")
        return root.expanduser()

    @property
    def resolved_llm_provider(self) -> str:
        explicit = self.llm_provider.strip().lower()
        if explicit:
            return explicit
        if self.llm_enabled and self.llm_base_url:
            return "openai_compatible"
        return "none"

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
        # Render and most container platforms inject PORT. Prefer it over the
        # local default so one image works on a host and on a managed platform.
        port=_env_int("PORT", _env_int("CLIPFORGE_WORKER_PORT", 8788)),
        token=_load_or_create_token(data_dir),
        max_upload_bytes=_env_int("CLIPFORGE_MAX_UPLOAD_MB", 2048) * 1024 * 1024,
        max_duration_ms=_env_int("CLIPFORGE_MAX_DURATION_MINUTES", 360) * 60 * 1000,
        min_free_bytes=_env_int("CLIPFORGE_DISK_RESERVE_MB", 512) * 1024 * 1024,
        whisper_model=os.environ.get("CLIPFORGE_WHISPER_MODEL", "small").strip() or "small",
        whisper_device=os.environ.get("CLIPFORGE_WHISPER_DEVICE", "cpu").strip() or "cpu",
        whisper_compute_type=os.environ.get("CLIPFORGE_WHISPER_COMPUTE_TYPE", "int8").strip() or "int8",
        llm_enabled=_env_bool("CLIPFORGE_LLM_ENABLED", False),
        llm_base_url=os.environ.get("CLIPFORGE_LLM_BASE_URL", "").strip(),
        llm_model=os.environ.get("CLIPFORGE_LLM_MODEL", "").strip(),
        llm_api_key=os.environ.get("CLIPFORGE_LLM_API_KEY", "").strip(),
        llm_timeout_seconds=_env_int("CLIPFORGE_LLM_TIMEOUT_SECONDS", 60),
        max_moments_per_request=max(1, _env_int("CLIPFORGE_MAX_MOMENTS_PER_REQUEST", 30)),
        youtube_api_key=os.environ.get("CLIPFORGE_YOUTUBE_API_KEY", "").strip(),
        youtube_suggest_enabled=_env_bool("CLIPFORGE_YOUTUBE_SUGGEST_ENABLED", False),
        trends_endpoint=os.environ.get("CLIPFORGE_TRENDS_ENDPOINT", "").strip(),
        trends_api_key=os.environ.get("CLIPFORGE_TRENDS_API_KEY", "").strip(),
        vidiq_enabled=_env_bool("CLIPFORGE_VIDIQ_ENABLED", False),
        twitch_client_id=os.environ.get("CLIPFORGE_TWITCH_CLIENT_ID", "").strip(),
        twitch_client_secret=os.environ.get("CLIPFORGE_TWITCH_CLIENT_SECRET", "").strip(),
        trend_cache_ttl_seconds=_env_int("CLIPFORGE_TREND_CACHE_TTL_SECONDS", 21_600),
        trend_stale_ttl_seconds=_env_int("CLIPFORGE_TREND_STALE_TTL_SECONDS", 604_800),
        trend_timeout_seconds=_env_int("CLIPFORGE_TREND_TIMEOUT_SECONDS", 12),
        cors_origins=_env_list("CLIPFORGE_CORS_ORIGINS", DEFAULT_CORS_ORIGINS),
        storage_backend=os.environ.get("CLIPFORGE_STORAGE_BACKEND", "local").strip() or "local",
        supabase_url=os.environ.get("CLIPFORGE_SUPABASE_URL", "").strip(),
        supabase_service_key=os.environ.get("CLIPFORGE_SUPABASE_SERVICE_KEY", "").strip(),
        supabase_bucket=os.environ.get("CLIPFORGE_SUPABASE_BUCKET", "clipforge").strip() or "clipforge",
        supabase_storage_prefix=os.environ.get("CLIPFORGE_SUPABASE_STORAGE_PREFIX", "/storage/v1"),
        supabase_timeout_seconds=_env_int("CLIPFORGE_SUPABASE_TIMEOUT_SECONDS", 60),
        autoproduce_enabled=_env_bool("CLIPFORGE_AUTOPRODUCE_ENABLED", True),
        autoproduce_clip_limit=max(1, _env_int("CLIPFORGE_AUTOPRODUCE_CLIP_LIMIT", 8)),
        autoproduce_repair_limit=max(0, _env_int("CLIPFORGE_AUTOPRODUCE_REPAIR_LIMIT", 2)),
        autoproduce_technical_retries=max(0, _env_int("CLIPFORGE_AUTOPRODUCE_TECHNICAL_RETRIES", 1)),
        pipeline_stage_timeout_seconds=max(60, _env_int("CLIPFORGE_PIPELINE_STAGE_TIMEOUT_SECONDS", 1_800)),
        pipeline_deadline_seconds=max(600, _env_int("CLIPFORGE_PIPELINE_DEADLINE_SECONDS", 4 * 60 * 60)),
        pipeline_stall_seconds=max(0, _env_int("CLIPFORGE_PIPELINE_STALL_SECONDS", 300)),
        pipeline_heartbeat_seconds=max(1.0, _env_int("CLIPFORGE_PIPELINE_HEARTBEAT_SECONDS", 10)),
        render_timeout_seconds=max(60, _env_int("CLIPFORGE_RENDER_TIMEOUT_SECONDS", 2_400)),
        llm_provider=os.environ.get("CLIPFORGE_LLM_PROVIDER", "").strip().lower(),
        llm_execution=os.environ.get("CLIPFORGE_LLM_EXECUTION", "local").strip().lower() or "local",
        llm_allow_remote=_env_bool("CLIPFORGE_LLM_ALLOW_REMOTE", False),
        llm_editorial_enabled=_env_bool("CLIPFORGE_LLM_EDITORIAL_ENABLED", False),
        llm_editorial_model=os.environ.get("CLIPFORGE_LLM_EDITORIAL_MODEL", "").strip(),
        export_dir=(
            _env_path("CLIPFORGE_EXPORT_DIR", Path.home() / "Documents" / "Clipforge")
            if os.environ.get("CLIPFORGE_EXPORT_DIR", "").strip()
            else None
        ),
    )
    return settings
