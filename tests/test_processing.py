from pathlib import Path

import pytest

from app.engine import ExecutionError
from app.policy import Policy
from app.media import MediaProbe, Stream
from app.engine import ProcessingPipeline
from app.engine import PromotionError
from app.media import fingerprint


class FakeExecutor:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def run(self, argv, **kwargs):
        output = Path(argv[-1])
        output.write_bytes(b"new")
        if self.fail:
            raise ExecutionError("failed")
        return type("Result", (), {"exit_code": 0})()


class FakePromoter:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def promote(self, **kwargs):
        if self.fail:
            raise PromotionError("deferred")
        return type("Promotion", (), {"final_path": str(kwargs["source_path"])})()


def fixture(tmp_path: Path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"old")
    probe = MediaProbe(
        path=str(source),
        format_names=("matroska",),
        duration_seconds=1,
        size=3,
        streams=(
            Stream(index=0, codec_type="video", codec_name="hevc"),
            Stream(index=1, codec_type="audio", codec_name="flac", channels=2, language="eng"),
        ),
    )
    return source, probe, Policy().evaluate(probe, "English")


def test_pipeline_deletes_cache_after_success(tmp_path: Path) -> None:
    source, probe, plan = fixture(tmp_path)
    pipeline = ProcessingPipeline(
        cache_root=tmp_path / "cache",
        executor=FakeExecutor(),
        promoter=FakePromoter(),
    )
    result = pipeline.process(
        plan_id="success",
        source_path=source,
        expected_source=fingerprint(source),
        source_probe=probe,
        plan=plan,
    )
    assert not Path(result.cache_path).exists()


def test_pipeline_deletes_incomplete_output_on_ffmpeg_failure(tmp_path: Path) -> None:
    source, probe, plan = fixture(tmp_path)
    cache_root = tmp_path / "cache"
    pipeline = ProcessingPipeline(
        cache_root=cache_root,
        executor=FakeExecutor(fail=True),
        promoter=FakePromoter(),
    )
    with pytest.raises(ExecutionError):
        pipeline.process(
            plan_id="ffmpeg-failure",
            source_path=source,
            expected_source=fingerprint(source),
            source_probe=probe,
            plan=plan,
        )
    assert not (cache_root / "ffmpeg-failure.mkv").exists()


def test_pipeline_deletes_complete_output_when_promotion_fails(tmp_path: Path) -> None:
    source, probe, plan = fixture(tmp_path)
    cache_root = tmp_path / "cache"
    pipeline = ProcessingPipeline(
        cache_root=cache_root,
        executor=FakeExecutor(),
        promoter=FakePromoter(fail=True),
    )
    with pytest.raises(PromotionError):
        pipeline.process(
            plan_id="promotion-failure",
            source_path=source,
            expected_source=fingerprint(source),
            source_probe=probe,
            plan=plan,
        )
    assert not (cache_root / "promotion-failure.mkv").exists()
