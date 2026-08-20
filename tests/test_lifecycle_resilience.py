from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.database import Database
from app.daemon import JobRunner
from app.domain import PlanSource, PlanState
from app.engine import ExecutionError, ExecutionResult
from app.policy import Policy, RemuxPlan
from app.media import MediaProbe, Stream
from app.engine import ProcessingPipeline
from app.engine import SafePromoter, SourceChanged
from app.media import fingerprint
from app.engine import ValidationError


def _make_source(path: Path, content: bytes = b"original-movie-data") -> tuple[Path, MediaProbe]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    probe = MediaProbe(
        path=str(path),
        format_names=("matroska",),
        duration_seconds=100.0,
        size=len(content),
        streams=(
            Stream(
                index=0,
                codec_type="video",
                codec_name="hevc",
                profile="Main 10",
                pixel_format="yuv420p10le",
            ),
            Stream(
                index=1,
                codec_type="audio",
                codec_name="dts",
                channels=6,
                bit_rate=1500000,
                language="eng",
            ),
        ),
    )
    return path, probe


def test_ffmpeg_failure_preserves_original_and_cleans_cache(tmp_path: Path) -> None:
    source_path, probe = _make_source(tmp_path / "movies" / "Movie.mkv")
    original_bytes = source_path.read_bytes()
    cache_root = tmp_path / "cache"

    class FailingExecutor:
        def run(self, argv, **kwargs):
            out = Path(argv[-1])
            out.write_bytes(b"corrupt-partial")
            raise ExecutionError("ffmpeg failed with exit code 1")

    pipeline = ProcessingPipeline(
        cache_root=cache_root,
        executor=FailingExecutor(),  # type: ignore[arg-type]
        promoter=MagicMock(),
    )

    plan = Policy().evaluate(probe, "eng")
    with pytest.raises(ExecutionError):
        pipeline.process(
            plan_id="job-1",
            source_path=source_path,
            expected_source=fingerprint(source_path),
            source_probe=probe,
            plan=plan,
        )

    # 1. Original file remains completely intact
    assert source_path.exists()
    assert source_path.read_bytes() == original_bytes

    # 2. No partial output left in cache
    assert not (cache_root / "job-1.mkv").exists()


def test_validation_failure_preserves_original_and_cleans_cache(tmp_path: Path) -> None:
    source_path, probe = _make_source(tmp_path / "movies" / "Movie.mkv")
    original_bytes = source_path.read_bytes()
    cache_root = tmp_path / "cache"

    class TruncatedOutputExecutor:
        def run(self, argv, **kwargs):
            out = Path(argv[-1])
            out.write_bytes(b"truncated")
            return ExecutionResult(0, False, {}, (), "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z")

    class MockProbeRunner:
        def probe(self, path: Path):
            # Report truncated duration
            return MediaProbe(
                path=str(path),
                format_names=("matroska",),
                duration_seconds=50.0,  # 50s != 100s source
                size=path.stat().st_size,
                streams=probe.streams,
            )

    promoter = SafePromoter(
        MockProbeRunner(),  # type: ignore[arg-type]
        duration_tolerance_seconds=2.0,
    )
    pipeline = ProcessingPipeline(
        cache_root=cache_root,
        executor=TruncatedOutputExecutor(),  # type: ignore[arg-type]
        promoter=promoter,
    )

    plan = Policy().evaluate(probe, "eng")
    with pytest.raises(ValidationError):
        pipeline.process(
            plan_id="job-val-fail",
            source_path=source_path,
            expected_source=fingerprint(source_path),
            source_probe=probe,
            plan=plan,
        )

    # Original file is preserved
    assert source_path.exists()
    assert source_path.read_bytes() == original_bytes

    # Partial cache file cleaned
    assert not (cache_root / "job-val-fail.mkv").exists()


def test_shutdown_cancellation_during_ffmpeg_preserves_original(tmp_path: Path) -> None:
    source_path, probe = _make_source(tmp_path / "movies" / "Movie.mkv")
    original_bytes = source_path.read_bytes()
    cache_root = tmp_path / "cache"
    cancel_event = threading.Event()

    class CancellableExecutor:
        def run(self, argv, cancel_event=None, **kwargs):
            out = Path(argv[-1])
            out.write_bytes(b"partial-stream")
            if cancel_event:
                cancel_event.set()
            raise ExecutionError("ffmpeg cancelled by shutdown")

    pipeline = ProcessingPipeline(
        cache_root=cache_root,
        executor=CancellableExecutor(),  # type: ignore[arg-type]
        promoter=MagicMock(),
    )

    plan = Policy().evaluate(probe, "eng")
    with pytest.raises(ExecutionError):
        pipeline.process(
            plan_id="job-shutdown",
            source_path=source_path,
            expected_source=fingerprint(source_path),
            source_probe=probe,
            plan=plan,
            cancel_event=cancel_event,
        )

    # Original is untouched
    assert source_path.exists()
    assert source_path.read_bytes() == original_bytes
    assert not (cache_root / "job-shutdown.mkv").exists()


def test_source_mutation_aborts_promotion_safely(tmp_path: Path) -> None:
    source_path, probe = _make_source(tmp_path / "movies" / "Movie.mkv")
    cache_root = tmp_path / "cache"
    cache_file = cache_root / "test.mkv"
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_file.write_bytes(b"new-valid-data")

    # Capture original fingerprint
    initial_fp = fingerprint(source_path)

    # Simulate source file being modified during encode (e.g. upgraded by user)
    source_path.write_bytes(b"modified-during-encode")

    promoter = SafePromoter(MagicMock())
    plan = Policy().evaluate(probe, "eng")

    with pytest.raises(SourceChanged):
        promoter.promote(
            plan_id="job-mutated",
            source_path=source_path,
            cache_output=cache_file,
            expected_source=initial_fp,
            source_probe=probe,
            plan=plan,
        )

    # Modified source was not overwritten
    assert source_path.read_bytes() == b"modified-during-encode"
