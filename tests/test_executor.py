import threading
from pathlib import Path

import pytest

from app.engine import ExecutionError, FFmpegExecutor


def executable(tmp_path: Path, script: str) -> Path:
    path = tmp_path / "ffmpeg"
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(0o755)
    return path


def test_executor_parses_progress_and_limits_log_tail(tmp_path: Path) -> None:
    ffmpeg = executable(
        tmp_path,
        "printf 'frame=1\\nout_time_ms=1000\\nprogress=continue\\nprogress=end\\n'\n",
    )
    updates = []
    result = FFmpegExecutor(log_tail_lines=3).run([str(ffmpeg)], on_progress=updates.append)
    assert result.exit_code == 0
    assert result.progress == {"progress": "end"}
    assert updates[0]["frame"] == "1"
    assert len(result.log_tail) == 3


def test_executor_reports_failure_tail(tmp_path: Path) -> None:
    ffmpeg = executable(tmp_path, "printf 'controlled failure\\n'\nexit 4\n")
    with pytest.raises(ExecutionError, match="controlled failure"):
        FFmpegExecutor().run([str(ffmpeg)])


def test_executor_cancels_process_even_without_output(tmp_path: Path) -> None:
    ffmpeg = executable(tmp_path, "sleep 30\n")
    cancel = threading.Event()
    cancel.set()
    result = FFmpegExecutor(terminate_timeout_seconds=1).run([str(ffmpeg)], cancel_event=cancel)
    assert result.cancelled
    assert result.exit_code != 0


def test_executor_rejects_non_ffmpeg_command() -> None:
    with pytest.raises(ExecutionError):
        FFmpegExecutor().run(["sh", "-c", "true"])

