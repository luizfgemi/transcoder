"""Configuration management, environment parsing, and logging filters.

Contract:
  - Responsibility: Parse, validate, and provide strongly typed runtime configuration (`Settings`)
    from environment variables, sanitize secrets, and configure structured logging.
  - Invariants: API keys and sensitive tokens are redacted in log outputs;
    strict type and range validations prevent invalid runtime startup.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigurationError(ValueError):
    """Raised when environment or runtime configuration values are invalid."""


def _required_key(env: Mapping[str, str], *keys: str) -> str:
    for key in keys:
        value = env.get(key, "").strip()
        if value:
            if len(value) < 16:
                raise ConfigurationError(f"{key} must be at least 16 characters")
            return value
    raise ConfigurationError(f"{' or '.join(keys)} is required")


def _integer(env: Mapping[str, str], key: str, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    raw = env.get(key, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise ConfigurationError(f"{key} must be an integer") from error
    if value < minimum:
        raise ConfigurationError(f"{key} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigurationError(f"{key} must be <= {maximum}")
    return value


def _float(env: Mapping[str, str], key: str, default: float, *, minimum: float = 0.0) -> float:
    raw = env.get(key, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as error:
        raise ConfigurationError(f"{key} must be a number") from error
    if value < minimum:
        raise ConfigurationError(f"{key} must be >= {minimum}")
    return value


def _boolean(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key, str(default)).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{key} must be true or false")


def _csv(env: Mapping[str, str], key: str, default: str) -> tuple[str, ...]:
    values = tuple(dict.fromkeys(part.strip().lower() for part in env.get(key, default).split(",") if part.strip()))
    if not values:
        raise ConfigurationError(f"{key} must contain at least one value")
    return values


DAY_ALIASES = {
    "seg": 0,
    "segunda": 0,
    "mon": 0,
    "monday": 0,
    "ter": 1,
    "terça": 1,
    "terca": 1,
    "tue": 1,
    "tuesday": 1,
    "qua": 2,
    "quarta": 2,
    "wed": 2,
    "wednesday": 2,
    "qui": 3,
    "quinta": 3,
    "thu": 3,
    "thursday": 3,
    "sex": 4,
    "sexta": 4,
    "fri": 4,
    "friday": 4,
    "sab": 5,
    "sábado": 5,
    "sabado": 5,
    "sat": 5,
    "saturday": 5,
    "dom": 6,
    "domingo": 6,
    "sun": 6,
    "sunday": 6,
}


def _days(env: Mapping[str, str]) -> tuple[int, ...]:
    raw = env.get("PROCESS_DAYS", "dom,seg,ter,qua,qui,sex,sab").strip().lower()
    days: list[int] = []
    for token in (part.strip() for part in raw.split(",") if part.strip()):
        if token.isdigit():
            day = int(token)
            if not 0 <= day <= 6:
                raise ConfigurationError(f"invalid day of week: {token}")
            days.append(day)
            continue
        if token not in DAY_ALIASES:
            raise ConfigurationError(f"invalid day alias: {token}")
        days.append(DAY_ALIASES[token])
    if not days:
        raise ConfigurationError("PROCESS_DAYS must contain at least one day")
    return tuple(dict.fromkeys(days))


TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _time(env: Mapping[str, str], key: str, default: str) -> time:
    raw = env.get(key, default).strip()
    if not TIME_PATTERN.match(raw):
        raise ConfigurationError(f"{key} must use HH:MM format (00:00-23:59)")
    try:
        hour, minute = (int(part) for part in raw.split(":", 1))
        return time(hour=hour, minute=minute)
    except Exception as error:
        raise ConfigurationError(f"{key} must use HH:MM format") from error


SECRET_KEY_FRAGMENTS = ("authorization", "api-key", "api_key", "token", "password", "secret")


def is_secret_key(value: str) -> bool:
    lowered = value.lower()
    return any(fragment in lowered for fragment in SECRET_KEY_FRAGMENTS)


class SecretRedactor(logging.Filter):
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = tuple(secret for secret in secrets if secret)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self._secrets:
            message = message.replace(secret, "<redacted>")
        record.msg = message
        record.args = ()
        return True


class HealthCheckFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "uvicorn.access":
            return True
        message = record.getMessage()
        return not ("GET /api/v1/health" in message and " 200" in message)


def configure_logging(*, api_key: str, level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    handler.addFilter(SecretRedactor((api_key,)))
    handler.addFilter(HealthCheckFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())


@dataclass(frozen=True, slots=True)
class Settings:
    api_key: str = "test-api-key-at-least-16-chars"
    host: str = "0.0.0.0"
    port: int = 8100
    api_port: int = 8100
    log_level: str = "INFO"
    database_path: Path = Path("/app/state/transcoder.sqlite")
    cache_path: Path = Path("/app/cache")
    movie_root: Path = Path("/movies")
    series_root: Path = Path("/series")
    plex_url: str = "http://plex:32400"
    plex_token_file: Path | None = None
    radarr_url: str = "http://radarr:7878"
    radarr_api_key: str = "radarr-key"
    sonarr_url: str = "http://sonarr:8989"
    sonarr_api_key: str = "sonarr-key"
    bazarr_url: str = "http://bazarr:6767"
    bazarr_api_key: str = "bazarr-key"
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo("America/Sao_Paulo"))
    retry_limit: int = 2
    retry_backoff_base_seconds: int = 60
    retry_backoff_multiplier: int = 4
    failure_retry_cooldown_days: int = 7
    stream_limit: int = 30
    video_copy_codecs: tuple[str, ...] = ("hevc", "h264")
    playback_maxrate_kbps: int = 26000
    nvenc_cq: int = 19
    nvenc_preset: str = "p7"
    nvenc_tune: str = "hq"
    nvenc_profile: str = "main10"
    nvenc_pix_fmt: str = "p010le"
    nvenc_spatial_aq: bool = True
    nvenc_temporal_aq: bool = True
    nvenc_aq_strength: int = 8
    nvenc_rc_lookahead: int = 32
    nvenc_b_ref_mode: str = "middle"
    audio_copy_codecs: tuple[str, ...] = ("aac", "ac3", "eac3")
    subtitle_keep_languages: tuple[str, ...] = ("por", "pt", "pob", "pt-br", "eng", "en")
    plex_activity_guard: bool = True
    allowed_extensions: tuple[str, ...] = (".mkv", ".mp4", ".avi", ".m4v", ".ts")
    process_days: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)
    window_start: time = time(3, 0)
    window_end: time = time(6, 0)
    max_jobs_per_run: int = 10
    empty_scan_cooldown_days: int = 7
    duration_tolerance_seconds: float = 2.0
    imports_run_immediately: bool = True
    max_concurrent_jobs: int = 1
    scan_concurrency: int = 1
    execution_enabled: bool = False
    automatic_scan_enabled: bool = False
    loop_interval_seconds: int = 5
    reconcile_deleted_grace_hours: int = 24
    file_stability_seconds: int = 60

    def __repr__(self) -> str:
        return f"Settings(api_port={self.api_port}, database_path={self.database_path}, api_key='***')"

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Settings":
        token_file = env.get("PLEX_TOKEN_FILE", "").strip()
        bot_token = env.get("TELEGRAM_BOT_TOKEN", "").strip() or None
        chat_id = env.get("TELEGRAM_CHAT_ID", "").strip() or None
        extensions = tuple(
            ext if ext.startswith(".") else f".{ext}"
            for ext in _csv(env, "ALLOWED_EXTENSIONS", "mkv,mp4,avi,m4v,ts")
        )
        tz_name = env.get("TZ", env.get("TIMEZONE", "America/Sao_Paulo")).strip()
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError as error:
            raise ConfigurationError(f"invalid timezone: {tz_name}") from error

        port = _integer(env, "API_PORT", _integer(env, "PORT", 8100))
        return cls(
            api_key=_required_key(env, "TRANSCODER_API_KEY", "API_KEY"),
            host=env.get("HOST", "0.0.0.0").strip(),
            port=port,
            api_port=port,
            log_level=env.get("LOG_LEVEL", "INFO").strip().upper(),
            database_path=Path(env.get("DATABASE_PATH", "/app/state/transcoder.sqlite")).resolve(),
            cache_path=Path(env.get("CACHE_PATH", "/app/cache")).resolve(),
            movie_root=Path(env.get("MOVIE_ROOT", "/movies")).resolve(),
            series_root=Path(env.get("SERIES_ROOT", "/series")).resolve(),
            plex_url=env.get("PLEX_URL", "http://plex:32400").strip().rstrip("/"),
            plex_token_file=Path(token_file).resolve() if token_file else None,
            radarr_url=env.get("RADARR_URL", "http://radarr:7878").strip().rstrip("/"),
            radarr_api_key=env.get("RADARR_API_KEY", "").strip(),
            sonarr_url=env.get("SONARR_URL", "http://sonarr:8989").strip().rstrip("/"),
            sonarr_api_key=env.get("SONARR_API_KEY", "").strip(),
            bazarr_url=env.get("BAZARR_URL", "http://bazarr:6767").strip().rstrip("/"),
            bazarr_api_key=env.get("BAZARR_API_KEY", "").strip(),
            telegram_bot_token=bot_token,
            telegram_chat_id=chat_id,
            timezone=tz,
            retry_limit=_integer(env, "RETRY_LIMIT", 2),
            retry_backoff_base_seconds=_integer(env, "RETRY_BACKOFF_BASE_SECONDS", 60),
            retry_backoff_multiplier=_integer(env, "RETRY_BACKOFF_MULTIPLIER", 4),
            failure_retry_cooldown_days=_integer(env, "FAILURE_RETRY_COOLDOWN_DAYS", 7, minimum=1),
            stream_limit=_integer(env, "STREAM_LIMIT", 30, minimum=1),
            video_copy_codecs=_csv(env, "VIDEO_COPY_CODECS", "hevc,h264"),
            playback_maxrate_kbps=_integer(env, "PLAYBACK_MAXRATE_KBPS", 26000, minimum=1000),
            nvenc_cq=_integer(env, "NVENC_CQ", _integer(env, "TRANSCODER_CQ", 19)),
            nvenc_preset=env.get("NVENC_PRESET", "p7").strip(),
            nvenc_tune=env.get("NVENC_TUNE", "hq").strip(),
            nvenc_profile=env.get("NVENC_PROFILE", "main10").strip(),
            nvenc_pix_fmt=env.get("NVENC_PIX_FMT", "p010le").strip(),
            nvenc_spatial_aq=_boolean(env, "NVENC_SPATIAL_AQ", True),
            nvenc_temporal_aq=_boolean(env, "NVENC_TEMPORAL_AQ", True),
            nvenc_aq_strength=_integer(env, "NVENC_AQ_STRENGTH", 8, minimum=1),
            nvenc_rc_lookahead=_integer(env, "NVENC_RC_LOOKAHEAD", 32, minimum=0),
            nvenc_b_ref_mode=env.get("NVENC_B_REF_MODE", "middle").strip(),
            audio_copy_codecs=_csv(env, "AUDIO_COPY_CODECS", "aac,ac3,eac3"),
            subtitle_keep_languages=_csv(env, "SUBTITLE_KEEP_LANGUAGES", "por,pt,pob,pt-br,eng,en"),
            plex_activity_guard=_boolean(env, "PLEX_ACTIVITY_GUARD", True),
            allowed_extensions=extensions,
            process_days=_days(env),
            window_start=_time(env, "WINDOW_START", "03:00"),
            window_end=_time(env, "WINDOW_END", "06:00"),
            max_jobs_per_run=_integer(env, "MAX_JOBS_PER_RUN", 10),
            empty_scan_cooldown_days=_integer(env, "EMPTY_SCAN_COOLDOWN_DAYS", 7, minimum=1),
            duration_tolerance_seconds=_float(env, "DURATION_TOLERANCE_SECONDS", 2.0, minimum=0.0),
            imports_run_immediately=_boolean(env, "IMPORTS_RUN_IMMEDIATELY", True),
            max_concurrent_jobs=_integer(env, "MAX_CONCURRENT_JOBS", 1, minimum=1, maximum=1),
            scan_concurrency=_integer(env, "SCAN_CONCURRENCY", 1, minimum=1, maximum=1),
            execution_enabled=_boolean(env, "EXECUTION_ENABLED", False),
            automatic_scan_enabled=_boolean(env, "AUTOMATIC_SCAN_ENABLED", False),
            loop_interval_seconds=_integer(env, "LOOP_INTERVAL_SECONDS", 5, minimum=1),
            reconcile_deleted_grace_hours=_integer(env, "RECONCILE_DELETED_GRACE_HOURS", 24, minimum=0),
            file_stability_seconds=_integer(env, "FILE_STABILITY_SECONDS", 60, minimum=0),
        )
