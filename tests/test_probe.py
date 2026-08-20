import json
from pathlib import Path

import pytest

from app.media import FFprobeRunner, MediaProbe, ProbeError


def test_parses_ffprobe_streams_and_chapters() -> None:
    probe = MediaProbe.from_ffprobe(
        "/library/movies/test.mkv",
        {
            "format": {"format_name": "matroska,webm", "duration": "12.5", "size": "1000"},
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "hevc"},
                {
                    "index": 1,
                    "codec_type": "audio",
                    "codec_name": "dts",
                    "channels": 8,
                    "tags": {"language": "JPN", "title": "Main"},
                    "disposition": {"default": 1, "comment": 1, "non_diegetic": 1},
                },
                {"index": 2, "codec_type": "subtitle", "codec_name": "subrip"},
                {"index": 3, "codec_type": "attachment", "codec_name": "ttf"},
            ],
            "chapters": [{"id": 0}],
        },
    )
    assert probe.format_names == ("matroska", "webm")
    assert probe.duration_seconds == 12.5
    assert probe.audio[0].language == "jpn"
    assert probe.audio[0].disposition.default
    assert probe.audio[0].disposition.comment
    assert probe.audio[0].disposition.non_diegetic
    assert probe.counted_streams == 3
    assert len(probe.attachments) == 1
    assert probe.chapter_count == 1


def test_ffprobe_runner_never_uses_shell_and_rejects_invalid_json(tmp_path: Path) -> None:
    fake = tmp_path / "ffprobe"
    fake.write_text("#!/bin/sh\nprintf 'not-json'\n")
    fake.chmod(0o755)
    media = tmp_path / "name with spaces; touch should-not-run.mkv"
    media.write_bytes(b"x")
    with pytest.raises(ProbeError, match="invalid JSON"):
        FFprobeRunner(str(fake)).probe(media)
    assert not (tmp_path / "should-not-run.mkv").exists()


def test_ffprobe_runner_reports_failure_without_stderr_dump(tmp_path: Path) -> None:
    fake = tmp_path / "ffprobe"
    fake.write_text("#!/bin/sh\nprintf 'safe failure\\n' >&2\nexit 2\n")
    fake.chmod(0o755)
    media = tmp_path / "input.mkv"
    media.write_bytes(b"x")
    with pytest.raises(ProbeError, match="safe failure"):
        FFprobeRunner(str(fake)).probe(media)
