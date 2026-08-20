from pathlib import Path

import pytest

from app.config import ConfigurationError, Settings


def base_env(tmp_path: Path) -> dict[str, str]:
    return {
        "TRANSCODER_API_KEY": "a-secure-test-key",
        "DATABASE_PATH": str(tmp_path / "state.sqlite"),
        "CACHE_PATH": str(tmp_path / "cache"),
        "MOVIE_ROOT": str(tmp_path / "movies"),
        "SERIES_ROOT": str(tmp_path / "series"),
        "TZ": "America/Sao_Paulo",
    }


def test_settings_parse_and_hide_secret(tmp_path: Path) -> None:
    settings = Settings.from_env(base_env(tmp_path))
    assert settings.api_port == 8100
    assert settings.stream_limit == 30
    assert settings.timezone.key == "America/Sao_Paulo"
    assert settings.api_key not in repr(settings)


def test_settings_reject_short_or_missing_key(tmp_path: Path) -> None:
    env = base_env(tmp_path)
    env["TRANSCODER_API_KEY"] = "short"
    with pytest.raises(ConfigurationError):
        Settings.from_env(env)


def test_settings_reject_invalid_boolean(tmp_path: Path) -> None:
    env = base_env(tmp_path)
    env["PLEX_ACTIVITY_GUARD"] = "perhaps"
    with pytest.raises(ConfigurationError):
        Settings.from_env(env)


def test_settings_parse_schedule_in_portuguese_and_english(tmp_path: Path) -> None:
    env = base_env(tmp_path)
    env.update(
        PROCESS_DAYS="SEG,terça,Wed,domingo",
        WINDOW_START="21:30",
        WINDOW_END="05:00",
        MAX_JOBS_PER_RUN="0",
    )
    settings = Settings.from_env(env)
    assert settings.process_days == (0, 1, 2, 6)
    assert settings.window_start.isoformat(timespec="minutes") == "21:30"
    assert settings.window_end.isoformat(timespec="minutes") == "05:00"
    assert settings.max_jobs_per_run == 0


def test_settings_accept_equal_window_bounds_as_full_day(tmp_path: Path) -> None:
    env = base_env(tmp_path)
    env["WINDOW_START"] = "00:00"
    env["WINDOW_END"] = "00:00"
    settings = Settings.from_env(env)
    assert settings.window_start == settings.window_end == settings.window_start.replace(hour=0, minute=0)


def test_settings_parse_duration_tolerance(tmp_path: Path) -> None:
    env = base_env(tmp_path)
    assert Settings.from_env(env).duration_tolerance_seconds == 2.0
    env["DURATION_TOLERANCE_SECONDS"] = "5"
    assert Settings.from_env(env).duration_tolerance_seconds == 5.0
    env["DURATION_TOLERANCE_SECONDS"] = "-1"
    with pytest.raises(ConfigurationError):
        Settings.from_env(env)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("WINDOW_START", "3:00"),
        ("WINDOW_END", "24:00"),
        ("PROCESS_DAYS", "neverday"),
        ("MAX_CONCURRENT_JOBS", "2"),
        ("SCAN_CONCURRENCY", "2"),
    ],
)
def test_settings_reject_invalid_schedule(tmp_path: Path, key: str, value: str) -> None:
    env = base_env(tmp_path)
    env[key] = value
    with pytest.raises(ConfigurationError):
        Settings.from_env(env)
