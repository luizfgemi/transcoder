from pathlib import Path

import pytest

from app.policy import NoProcessingRequired, build_ffmpeg_argv
from app.policy import Criterion, Policy
from app.media import Disposition, MediaProbe, Stream


def stream(
    index: int,
    kind: str,
    codec: str,
    *,
    language: str = "und",
    channels: int | None = None,
    default: bool = False,
    forced: bool = False,
    title: str | None = None,
    bit_rate: int | None = None,
    side_data_types: tuple[str, ...] = (),
) -> Stream:
    return Stream(
        index=index,
        codec_type=kind,
        codec_name=codec,
        channels=channels,
        language=language,
        title=title,
        disposition=Disposition(default=default, forced=forced),
        bit_rate=bit_rate,
        side_data_types=side_data_types,
    )


def probe(*streams: Stream, path: str = "/library/movies/test.mkv") -> MediaProbe:
    return MediaProbe(
        path=path,
        format_names=("matroska", "webm"),
        duration_seconds=100.0,
        size=1000,
        streams=tuple(streams),
        chapter_count=1,
    )


def test_compatible_file_is_byte_preserving_noop() -> None:
    media = probe(
        stream(0, "video", "hevc", default=True),
        stream(1, "audio", "aac", language="eng", channels=6, default=True),
        stream(2, "subtitle", "subrip", language="eng"),
    )
    plan = Policy().evaluate(media, "English")
    assert plan.compliant
    assert not plan.needs_processing
    assert plan.target_container is None
    with pytest.raises(NoProcessingRequired):
        build_ffmpeg_argv(plan, Path(media.path), Path("/cache/out.mkv"))


def test_transcodes_only_incompatible_audio_and_downmixes_to_51() -> None:
    media = probe(
        stream(0, "video", "hevc"),
        stream(1, "audio", "dts", language="eng", channels=8),
        stream(2, "audio", "aac", language="por", channels=2),
    )
    plan = Policy().evaluate(media, "English")
    assert plan.criteria == (Criterion.AUDIO,)
    assert plan.audio[0].codec == "eac3"
    assert plan.audio[0].channels == 6
    assert plan.audio[0].bitrate_kbps == 768
    assert plan.audio[1].codec == "copy"
    argv = build_ffmpeg_argv(plan, Path(media.path), Path("/cache/out.mkv"))
    assert argv[argv.index("-c:a:0") + 1] == "eac3"
    assert "-c:a:1" not in argv
    assert argv[-2:] == ["matroska", "/cache/out.mkv"]


def test_compatible_71_audio_is_transcoded_for_downmix() -> None:
    media = probe(stream(0, "video", "hevc"), stream(1, "audio", "aac", channels=8))
    plan = Policy().evaluate(media, "English")
    assert plan.criteria == (Criterion.AUDIO,)
    assert plan.audio[0].transcoded
    assert plan.audio[0].channels == 6


def test_compatible_original_audio_replaces_duplicate_truehd_without_transcoding() -> None:
    media = probe(
        stream(0, "video", "hevc"),
        stream(1, "audio", "truehd", language="eng", channels=8, default=True),
        stream(2, "audio", "ac3", language="eng", channels=6),
    )
    plan = Policy().evaluate(media, "English")
    assert plan.criteria == (Criterion.DUPLICATE_ORIGINAL_AUDIO,)
    assert [audio.input_index for audio in plan.audio] == [2]
    assert plan.audio[0].codec == "copy"
    assert plan.audio[0].transcoded is False
    assert plan.output_counted_streams == 2
    argv = build_ffmpeg_argv(plan, Path(media.path), Path("/cache/out.mkv"))
    assert "0:2" in argv
    assert "0:1" not in argv
    assert "-c:a:0" not in argv


def test_moves_original_language_first_and_normalizes_default_only_when_processing() -> None:
    media = probe(
        stream(0, "video", "hevc"),
        stream(1, "audio", "aac", language="eng", channels=6, default=True),
        stream(2, "audio", "aac", language="jpn", channels=2),
        stream(3, "subtitle", "ass", language="eng"),
        stream(4, "attachment", "ttf"),
    )
    plan = Policy().evaluate(media, "Japanese")
    assert plan.criteria == (Criterion.AUDIO_ORDER,)
    assert [audio.input_index for audio in plan.audio] == [2, 1]
    assert "default" in plan.audio[0].dispositions
    assert "default" not in plan.audio[1].dispositions
    assert plan.attachment_indices == (4,)


def test_audio_dispositions_are_preserved_while_default_moves() -> None:
    media = MediaProbe(
        path="/library/movies/test.mkv",
        format_names=("matroska",),
        duration_seconds=10,
        size=100,
        streams=(
            stream(0, "video", "hevc"),
            Stream(
                index=1,
                codec_type="audio",
                codec_name="aac",
                channels=2,
                language="eng",
                disposition=Disposition(default=True, comment=True, non_diegetic=True),
            ),
            Stream(
                index=2,
                codec_type="audio",
                codec_name="aac",
                channels=2,
                language="jpn",
                disposition=Disposition(original=True),
            ),
        ),
    )
    plan = Policy().evaluate(media, "Japanese")
    assert set(plan.audio[0].dispositions) == {"default", "original"}
    assert set(plan.audio[1].dispositions) == {"comment", "non_diegetic"}


def test_missing_original_language_match_does_not_remux() -> None:
    media = probe(stream(0, "video", "h264"), stream(1, "audio", "aac", language="und", channels=2))
    plan = Policy().evaluate(media, "Japanese")
    assert plan.compliant
    assert not plan.preferred_language_match


def test_stream_limit_keeps_only_prioritized_portuguese_and_english() -> None:
    streams = [stream(0, "video", "hevc"), stream(1, "audio", "eac3", language="eng", channels=6)]
    languages = ["spa"] * 10 + ["eng"] * 10 + ["por"] * 9
    for offset, language in enumerate(languages, start=2):
        streams.append(
            stream(
                offset,
                "subtitle",
                "subrip",
                language=language,
                forced=offset == 12,
            )
        )
    media = probe(*streams)
    plan = Policy().evaluate(media, "English")
    assert plan.criteria == (Criterion.STREAM_LIMIT,)
    assert plan.output_counted_streams == 21
    assert len(plan.removed_subtitle_indices) == 10
    assert all(index >= 12 for index in plan.subtitle_indices)


def test_stream_limit_trims_pt_en_to_capacity_in_priority_order() -> None:
    streams = [stream(0, "video", "hevc")]
    streams.extend(stream(i, "audio", "eac3", language="eng", channels=6) for i in range(1, 6))
    for index in range(6, 40):
        language = "por" if index % 2 else "eng"
        streams.append(stream(index, "subtitle", "subrip", language=language, forced=index == 39))
    plan = Policy().evaluate(probe(*streams), "English")
    assert plan.output_counted_streams == 30
    assert plan.subtitle_indices[0] == 39


def test_video_plus_audio_over_limit_is_policy_exception() -> None:
    streams = [stream(0, "video", "hevc")]
    streams.extend(stream(i, "audio", "aac", language="eng", channels=2) for i in range(1, 31))
    plan = Policy().evaluate(probe(*streams), "English")
    assert plan.policy_exception
    assert not plan.needs_processing
    assert plan.output_counted_streams == 31


def test_combined_criteria_generate_one_safe_argv_without_sma_tags() -> None:
    streams = [
        stream(0, "video", "hevc"),
        stream(1, "audio", "ac3", language="eng", channels=6, default=True),
        stream(2, "audio", "flac", language="jpn", channels=6, title="Japanese lossless"),
    ]
    streams.extend(stream(i, "subtitle", "subrip", language="spa") for i in range(3, 35))
    plan = Policy().evaluate(probe(*streams), "Japanese")
    assert plan.criteria == (Criterion.AUDIO, Criterion.AUDIO_ORDER, Criterion.STREAM_LIMIT)
    assert [audio.input_index for audio in plan.audio] == [2, 1]
    assert plan.output_counted_streams == 3
    argv = build_ffmpeg_argv(plan, Path(plan.path), Path("/cache/job.mkv"))
    joined = " ".join(argv)
    assert argv.count("ffmpeg") == 1
    assert "encoding_tool" not in joined
    assert "hvc1" not in joined
    assert "FHD" not in joined
    assert "-map_metadata 0" in joined
    assert "-map_chapters 0" in joined


def test_dovi_video_is_transcoded_regardless_of_bitrate() -> None:
    media = probe(
        stream(
            0,
            "video",
            "hevc",
            bit_rate=35_000_000,
            side_data_types=("DOVI configuration record",),
        ),
        stream(1, "audio", "aac", language="eng", channels=6, default=True),
    )
    plan = Policy().evaluate(media, "English")
    assert plan.video.transcoded
    assert plan.video.codec == "hevc_nvenc"
    assert not plan.compliant
    assert plan.needs_processing


@pytest.mark.parametrize("bitrate", [5_000_000, 20_000_000, 35_000_000, 80_000_000])
def test_dovi_low_and_high_bitrate_both_transcoded(bitrate: int) -> None:
    media = probe(
        stream(
            0,
            "video",
            "hevc",
            bit_rate=bitrate,
            side_data_types=("DOVI configuration record",),
        ),
        stream(1, "audio", "aac", language="eng", channels=6, default=True),
    )
    plan = Policy().evaluate(media, "English")
    assert plan.video.transcoded
    assert plan.video.codec == "hevc_nvenc"
    assert plan.needs_processing


def test_policy_signature_changes_with_policy() -> None:
    assert Policy().signature == Policy().signature
    assert Policy(stream_limit=31).signature != Policy().signature
    assert Policy().signature_for("Japanese") != Policy().signature_for("English")
