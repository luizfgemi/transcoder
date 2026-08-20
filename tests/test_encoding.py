from __future__ import annotations

from pathlib import Path

from app.policy import build_ffmpeg_argv
from app.policy import AudioOutput, Criterion, Policy, RemuxPlan, VideoOutput
from app.media import Disposition, MediaProbe, Stream


def _mock_probe(codec_name: str = "hevc", bit_rate: int | None = 10_000_000) -> MediaProbe:
    return MediaProbe(
        path="/media/movies/Test.mkv",
        format_names=("matroska",),
        duration_seconds=7200.0,
        size=10_000_000_000,
        streams=(
            Stream(
                index=0,
                codec_type="video",
                codec_name=codec_name,
                bit_rate=bit_rate,
                pixel_format="yuv420p10le",
                color_primaries="bt2020",
                color_transfer="smpte2084",
                color_space="bt2020nc",
                color_range="tv",
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


def test_policy_video_transcode_needed_for_incompatible_codec() -> None:
    policy = Policy(video_copy_codecs=("hevc",))
    probe = _mock_probe(codec_name="vc1")
    plan = policy.evaluate(probe, "eng")
    assert Criterion.VIDEO in plan.criteria
    assert plan.video.transcoded is True
    assert plan.video.codec == "hevc_nvenc"
    assert plan.video.cq == 19
    assert plan.video.preset == "p7"
    assert plan.video.tune == "hq"
    assert plan.video.profile == "main10"
    assert plan.video.pix_fmt == "p010le"
    assert plan.video.color_primaries == "bt2020"


def test_policy_video_copy_for_compliant_hevc() -> None:
    policy = Policy(video_copy_codecs=("hevc",), playback_maxrate_kbps=26000)
    probe = _mock_probe(codec_name="hevc", bit_rate=15_000_000)
    plan = policy.evaluate(probe, "eng")
    assert plan.video.transcoded is False


def test_policy_video_transcode_when_bitrate_exceeds_maxrate() -> None:
    policy = Policy(video_copy_codecs=("hevc",), playback_maxrate_kbps=26000)
    probe = _mock_probe(codec_name="hevc", bit_rate=35_000_000)
    plan = policy.evaluate(probe, "eng")
    assert Criterion.VIDEO in plan.criteria
    assert plan.video.transcoded is True


def test_builder_nvenc_command() -> None:
    policy = Policy(video_copy_codecs=("hevc",), nvenc_cq=18)
    probe = _mock_probe(codec_name="mpeg2video")
    plan = policy.evaluate(probe, "eng")
    assert plan.video.transcoded is True

    argv = build_ffmpeg_argv(
        plan=plan,
        input_path=Path("/input/movie.mkv"),
        output_path=Path("/output/movie.mkv"),
    )

    assert "ffmpeg" in argv
    assert "-i" in argv and "/input/movie.mkv" in argv
    assert "-map" in argv and "0:0" in argv
    assert "-c:v" in argv
    v_idx = argv.index("-c:v")
    assert argv[v_idx + 1] == "hevc_nvenc"
    assert "-preset" in argv and "p7" in argv
    assert "-tune" in argv and "hq" in argv
    assert "-profile:v" in argv and "main10" in argv
    assert "-pix_fmt" in argv and "p010le" in argv
    assert "-rc" in argv and "vbr" in argv
    assert "-cq:v" in argv and "18" in argv
    assert "-maxrate:v" in argv and "26000k" in argv
    assert "-spatial-aq" in argv and "1" in argv
    assert "-temporal-aq" in argv and "1" in argv
    assert "-color_primaries" in argv and "bt2020" in argv
    assert "-color_trc" in argv and "smpte2084" in argv
    assert "-colorspace" in argv and "bt2020nc" in argv


def test_builder_transcoded_video_drops_inherited_stream_statistics_tags() -> None:
    policy = Policy(video_copy_codecs=("hevc",))
    probe = _mock_probe(codec_name="mpeg2video")
    plan = policy.evaluate(probe, "eng")
    assert plan.video.transcoded is True

    argv = build_ffmpeg_argv(
        plan=plan,
        input_path=Path("/input/movie.mkv"),
        output_path=Path("/output/movie.mkv"),
    )

    assert ("-map_metadata:s:v:0", "-1") in list(zip(argv, argv[1:]))


def test_builder_copied_video_keeps_stream_statistics_tags() -> None:
    plan = RemuxPlan(
        path="/media/movies/Test.mkv",
        policy_signature="sig",
        criteria=(Criterion.AUDIO,),
        compliant=False,
        policy_exception=None,
        target_container="matroska",
        video_indices=(0,),
        audio=(
            AudioOutput(
                input_index=1,
                codec="eac3",
                channels=6,
                bitrate_kbps=768,
                language="eng",
                title=None,
                dispositions=("default",),
                transcoded=True,
            ),
        ),
        subtitle_indices=(),
        removed_subtitle_indices=(),
        attachment_indices=(),
        input_counted_streams=2,
        output_counted_streams=2,
        preferred_languages=("eng",),
        preferred_language_match=True,
        video=VideoOutput(transcoded=False),
    )

    argv = build_ffmpeg_argv(
        plan=plan,
        input_path=Path("/input/movie.mkv"),
        output_path=Path("/output/movie.mkv"),
    )

    assert "-map_metadata:s:v:0" not in argv


def test_builder_copy_video_command() -> None:
    plan = RemuxPlan(
        path="/media/movies/Test.mkv",
        policy_signature="sig",
        criteria=(Criterion.AUDIO,),
        compliant=False,
        policy_exception=None,
        target_container="matroska",
        video_indices=(0,),
        audio=(
            AudioOutput(
                input_index=1,
                codec="eac3",
                channels=6,
                bitrate_kbps=768,
                language="eng",
                title=None,
                dispositions=("default",),
                transcoded=True,
            ),
        ),
        subtitle_indices=(),
        removed_subtitle_indices=(),
        attachment_indices=(),
        input_counted_streams=2,
        output_counted_streams=2,
        preferred_languages=("eng",),
        preferred_language_match=True,
        video=VideoOutput(transcoded=False),
    )

    argv = build_ffmpeg_argv(
        plan=plan,
        input_path=Path("/input/movie.mkv"),
        output_path=Path("/output/movie.mkv"),
    )

    assert "-c" in argv and "copy" in argv
    assert "-c:v" not in argv
    assert "-c:a:0" in argv and "eac3" in argv
