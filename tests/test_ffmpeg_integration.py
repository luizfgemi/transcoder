import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from app.engine import FFmpegExecutor
from app.policy import build_ffmpeg_argv
from app.policy import Criterion, Policy
from app.media import FFprobeRunner
from app.engine import SafePromoter, complete_extension_migration
from app.media import fingerprint
from app.engine import validate_output


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg tools are not installed",
)
def test_real_ffmpeg_fixture_validates_and_promotes(tmp_path: Path) -> None:
    source = tmp_path / "fixture.mkv"
    cache = tmp_path / "cache-output.mkv"
    generation = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=128x72:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "1",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "mpeg4",
            "-q:v",
            "5",
            "-c:a",
            "flac",
            "-metadata",
            "title=Remux Dispatcher Fixture",
            "-metadata:s:a:0",
            "language=jpn",
            "-metadata:s:a:0",
            "title=Japanese Fixture",
            "-f",
            "matroska",
            str(source),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert generation.returncode == 0, generation.stderr

    runner = FFprobeRunner(timeout_seconds=30)
    source_probe = runner.probe(source)
    plan = Policy().evaluate(source_probe, "Japanese")
    assert plan.criteria == (Criterion.AUDIO,)
    argv = build_ffmpeg_argv(plan, source, cache)
    execution = FFmpegExecutor().run(argv)
    assert execution.exit_code == 0
    output_probe = runner.probe(cache)
    validate_output(source_probe, output_probe, plan, duration_tolerance_seconds=2)

    expected = fingerprint(source)
    promotion = SafePromoter(runner).promote(
        plan_id=str(uuid.uuid4()),
        source_path=source,
        cache_output=cache,
        expected_source=expected,
        source_probe=source_probe,
        plan=plan,
    )
    assert Path(promotion.final_path) == source
    final_probe = runner.probe(source)
    assert final_probe.audio[0].codec_name == "eac3"
    assert final_probe.video[0].codec_name == source_probe.video[0].codec_name
    assert Path(promotion.backup_path).exists()
    complete_extension_migration(promotion)
