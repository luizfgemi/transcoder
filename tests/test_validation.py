from dataclasses import replace

import pytest

from app.policy import Policy
from app.media import Disposition, MediaProbe, Stream
from app.engine import ValidationError, validate_output


def source_and_plan():
    source = MediaProbe(
        path="/library/movies/test.mkv",
        format_names=("matroska", "webm"),
        duration_seconds=100,
        size=1000,
        streams=(
            Stream(
                index=0,
                codec_type="video",
                codec_name="hevc",
                profile="Main 10",
                pixel_format="yuv420p10le",
                color_space="bt2020nc",
                color_transfer="smpte2084",
                color_primaries="bt2020",
                side_data_types=("DOVI configuration record", "Mastering display metadata"),
                metadata=(("title", "Video"),),
            ),
            Stream(
                index=1,
                codec_type="audio",
                codec_name="dts",
                channels=8,
                language="eng",
                title="Main",
                disposition=Disposition(default=True, original=True),
            ),
            Stream(
                index=2,
                codec_type="subtitle",
                codec_name="ass",
                language="eng",
                metadata=(("language", "eng"), ("title", "English")),
            ),
            Stream(
                index=3,
                codec_type="attachment",
                codec_name="ttf",
                metadata=(("filename", "font.ttf"), ("mimetype", "application/x-truetype-font")),
            ),
        ),
        chapter_count=2,
        format_metadata=(("title", "Movie"),),
    )
    return source, Policy().evaluate(source, "English")


def valid_output(source: MediaProbe) -> MediaProbe:
    return MediaProbe(
        path="/cache/output.mkv",
        format_names=("matroska", "webm"),
        duration_seconds=100.4,
        size=900,
        streams=(
            replace(
                source.video[0],
                index=0,
                side_data_types=tuple(s for s in source.video[0].side_data_types if "dovi" not in s.lower()),
            ),
            Stream(
                index=1,
                codec_type="audio",
                codec_name="eac3",
                channels=6,
                bit_rate=768000,
                language="eng",
                title="Main",
                disposition=Disposition(default=True, original=True),
            ),
            replace(source.subtitles[0], index=2),
            replace(source.attachments[0], index=3),
        ),
        chapter_count=2,
        format_metadata=(("ENCODER", "Lavf"), ("title", "Movie")),
    )


def test_validates_hdr_dv_metadata_chapters_attachments_and_plan() -> None:
    source, plan = source_and_plan()
    result = validate_output(source, valid_output(source), plan, duration_tolerance_seconds=2)
    assert result.duration_delta_seconds == pytest.approx(0.4)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda output: replace(output, duration_seconds=105), "duration delta"),
        (
            lambda output: replace(
                output,
                streams=(replace(output.video[0], color_transfer="bt709"),) + output.streams[1:],
            ),
            "color_transfer changed",
        ),
        (
            lambda output: replace(output, streams=output.streams[:-1]),
            "attachment count",
        ),
    ],
)
def test_rejects_semantic_differences(mutate, message: str) -> None:
    source, plan = source_and_plan()
    with pytest.raises(ValidationError, match=message):
        validate_output(source, mutate(valid_output(source)), plan, duration_tolerance_seconds=2)


def test_ignores_muxer_statistics_metadata_artifacts() -> None:
    source, plan = source_and_plan()
    output = valid_output(source)
    output = replace(
        output,
        format_metadata=(
            ("title", "Movie"),
            ("CREATION_TIME", "2024-01-01T00:00:00.000000Z"),
            ("Writing frontend", "StaxRip v2.0.0.0"),
            ("BPS", "123456"),
            ("_STATISTICS_TAGS", "BPS DURATION NUMBER_OF_FRAMES"),
        ),
        streams=(
            replace(
                output.video[0],
                codec_tag="[0][0][0][0]",
                metadata=(
                    ("title", "Video"),
                    ("BPS", "123456"),
                    ("NUMBER_OF_FRAMES", "2400"),
                    ("_STATISTICS_WRITING_APP", "mkvmerge v90"),
                ),
            ),
        ) + output.streams[1:],
    )
    validate_output(source, output, plan, duration_tolerance_seconds=2)


def test_ignores_writing_application_metadata_and_key_order() -> None:
    source, plan = source_and_plan()
    source = replace(
        source,
        format_metadata=(("title", "Movie"), ("genre", "Drama")),
    )
    output = replace(
        valid_output(source),
        format_metadata=(
            ("GENRE", "Drama"),
            ("TITLE", "Movie"),
            ("ENCODER", "Lavf"),
        ),
        streams=(
            replace(
                valid_output(source).video[0],
                index=0,
                metadata=(
                    ("Writing library", "IFME v7.7 amd64 windows"),
                    ("Writing application", "Internet Friendly Media Encoder"),
                    ("title", "Video"),
                ),
            ),
        ) + valid_output(source).streams[1:],
    )
    validate_output(source, output, plan, duration_tolerance_seconds=2)


def test_ignores_codec_tag_normalization() -> None:
    source, plan = source_and_plan()
    output = valid_output(source)
    output = replace(
        output,
        streams=(replace(output.video[0], codec_tag="V_MPEGH/ISO/HEVC"),) + output.streams[1:],
    )
    validate_output(source, output, plan, duration_tolerance_seconds=2)


def test_transcoded_output_must_not_contain_dovi() -> None:
    source, plan = source_and_plan()
    assert plan.video.transcoded
    invalid_output = replace(
        valid_output(source),
        streams=(
            replace(source.video[0], index=0),  # retains source DOVI side data
        ) + valid_output(source).streams[1:],
    )
    with pytest.raises(ValidationError, match="DOVI configuration record still present"):
        validate_output(source, invalid_output, plan, duration_tolerance_seconds=2)


def test_copied_output_must_retain_dovi() -> None:
    source, plan = source_and_plan()
    copy_plan = replace(plan, video=replace(plan.video, transcoded=False))

    # Missing DOVI should fail for copied stream
    output_without_dovi = valid_output(source)
    with pytest.raises(ValidationError, match="DOVI configuration record missing"):
        validate_output(source, output_without_dovi, copy_plan, duration_tolerance_seconds=2)

    # Retained DOVI should pass for copied stream
    output_with_dovi = replace(
        valid_output(source),
        streams=(
            replace(source.video[0], index=0),
        ) + valid_output(source).streams[1:],
    )
    validate_output(source, output_with_dovi, copy_plan, duration_tolerance_seconds=2)
