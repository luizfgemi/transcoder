"""Transcoding decision engine, policy rulesets, and FFmpeg command compilation.

Contract:
  - Responsibility: Evaluate `MediaProbe` inspection against configured encoding policy (`Policy.evaluate`)
    and produce deterministic execution plans (`RemuxPlan`) with FFmpeg arguments (`build_ffmpeg_argv`).
  - Inputs: `MediaProbe` metadata and user/Arr preferred audio language code.
  - Outputs: `RemuxPlan` (criteria, target streams, compliance status) and policy SHA-256 signatures.
  - Invariants:
      * `RULESET_VERSION` (currently 4) forms part of the cache signature.
      * DOVI sources unconditionally trigger video transcode (HEVC NVENC, stripped DOVI).
      * Audio is normalized to EAC3 5.1 when incompatible or redundant.
      * Subtitle tracks in preferred languages (por/eng) are retained up to stream limit.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from app.media import MediaProbe, Stream


LANGUAGE_ALIASES: dict[str, frozenset[str]] = {
    "english": frozenset({"eng", "en"}),
    "eng": frozenset({"eng", "en"}),
    "en": frozenset({"eng", "en"}),
    "portuguese": frozenset({"por", "pt", "pob", "pt-br", "pt_br"}),
    "portuguese (brazil)": frozenset({"por", "pt", "pob", "pt-br", "pt_br"}),
    "brazilian portuguese": frozenset({"por", "pt", "pob", "pt-br", "pt_br"}),
    "por": frozenset({"por", "pt", "pob", "pt-br", "pt_br"}),
    "pt": frozenset({"por", "pt", "pob", "pt-br", "pt_br"}),
    "japanese": frozenset({"jpn", "jap", "ja"}),
    "jpn": frozenset({"jpn", "jap", "ja"}),
    "french": frozenset({"fra", "fre", "fr"}),
    "fra": frozenset({"fra", "fre", "fr"}),
    "spanish": frozenset({"spa", "es"}),
    "spa": frozenset({"spa", "es"}),
    "italian": frozenset({"ita", "it"}),
    "ita": frozenset({"ita", "it"}),
    "german": frozenset({"deu", "ger", "de"}),
    "deu": frozenset({"deu", "ger", "de"}),
    "korean": frozenset({"kor", "ko"}),
    "kor": frozenset({"kor", "ko"}),
    "chinese": frozenset({"zho", "chi", "zh"}),
    "zho": frozenset({"zho", "chi", "zh"}),
}

PORTUGUESE = LANGUAGE_ALIASES["por"]
ENGLISH = LANGUAGE_ALIASES["eng"]


class Criterion(StrEnum):
    VIDEO = "video_incompatible"
    AUDIO = "audio_incompatible"
    AUDIO_ORDER = "audio_original_not_first"
    DUPLICATE_ORIGINAL_AUDIO = "duplicate_original_audio_optimized"
    STREAM_LIMIT = "stream_limit_exceeded"


@dataclass(frozen=True, slots=True)
class VideoOutput:
    transcoded: bool = False
    codec: str = "hevc_nvenc"
    target_codec_name: str = "hevc"
    preset: str = "p7"
    tune: str = "hq"
    profile: str = "main10"
    pix_fmt: str = "p010le"
    cq: int = 19
    maxrate_kbps: int = 26000
    bufsize_kbps: int = 52000
    spatial_aq: bool = True
    temporal_aq: bool = True
    aq_strength: int = 8
    rc_lookahead: int = 32
    b_ref_mode: str = "middle"
    color_primaries: str | None = None
    color_transfer: str | None = None
    color_space: str | None = None
    color_range: str | None = None


@dataclass(frozen=True, slots=True)
class AudioOutput:
    input_index: int
    codec: str
    channels: int | None
    bitrate_kbps: int | None
    language: str
    title: str | None
    dispositions: tuple[str, ...]
    transcoded: bool


@dataclass(frozen=True, slots=True)
class RemuxPlan:
    path: str
    policy_signature: str
    criteria: tuple[Criterion, ...]
    compliant: bool
    policy_exception: str | None
    target_container: str | None
    video_indices: tuple[int, ...]
    audio: tuple[AudioOutput, ...]
    subtitle_indices: tuple[int, ...]
    removed_subtitle_indices: tuple[int, ...]
    attachment_indices: tuple[int, ...]
    input_counted_streams: int
    output_counted_streams: int
    preferred_languages: tuple[str, ...]
    preferred_language_match: bool
    video: VideoOutput = field(default_factory=VideoOutput)

    @property
    def needs_processing(self) -> bool:
        return not self.compliant and self.policy_exception is None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["criteria"] = [criterion.value for criterion in self.criteria]
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RemuxPlan":
        video_raw = raw.get("video")
        video = VideoOutput(**video_raw) if video_raw else VideoOutput()
        return cls(
            path=str(raw["path"]),
            policy_signature=str(raw["policy_signature"]),
            criteria=tuple(Criterion(value) for value in raw.get("criteria") or ()),
            compliant=bool(raw.get("compliant")),
            policy_exception=raw.get("policy_exception"),
            target_container=raw.get("target_container"),
            video_indices=tuple(raw.get("video_indices") or ()),
            audio=tuple(
                AudioOutput(
                    **{
                        **item,
                        "dispositions": tuple(item.get("dispositions") or ()),
                    }
                )
                for item in raw.get("audio") or ()
            ),
            subtitle_indices=tuple(raw.get("subtitle_indices") or ()),
            removed_subtitle_indices=tuple(raw.get("removed_subtitle_indices") or ()),
            attachment_indices=tuple(raw.get("attachment_indices") or ()),
            input_counted_streams=int(raw.get("input_counted_streams") or 0),
            output_counted_streams=int(raw.get("output_counted_streams") or 0),
            preferred_languages=tuple(raw.get("preferred_languages") or ()),
            preferred_language_match=bool(raw.get("preferred_language_match")),
            video=video,
        )


@dataclass(frozen=True, slots=True)
class Policy:
    RULESET_VERSION: ClassVar[int] = 4
    video_copy_codecs: tuple[str, ...] = ("hevc", "h264", "mpeg4", "mpeg2video", "vc1", "vp9", "av1")
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
    audio_target_codec: str = "eac3"
    audio_max_channels: int = 6
    audio_channel_bitrate_kbps: int = 128
    audio_max_bitrate_kbps: int = 768
    stream_limit: int = 30
    subtitle_keep_languages: tuple[str, ...] = ("por", "pt", "pob", "pt-br", "eng", "en")

    @property
    def signature(self) -> str:
        return self.signature_for("eng")

    def signature_for(self, preferred_language: str | None) -> str:
        payload = json.dumps(
            {
                "rulesetVersion": self.RULESET_VERSION,
                "policy": asdict(self),
                "preferredLanguages": sorted(normalize_language(preferred_language or "eng")),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def evaluate(self, probe: MediaProbe, preferred_language: str | None) -> RemuxPlan:
        """Evaluate a media probe against transcoding policy and produce an execution plan.

        Contract:
          - Video: forces transcode (HEVC NVENC) if stream has Dolby Vision (DOVI),
            if codec is unsupported for direct stream copy, or if bitrate > playback_maxrate_kbps.
          - Audio: preserves preferred language tracks, converts non-compliant audio to EAC3 5.1,
            and drops redundant non-compliant tracks when compliant tracks exist.
          - Subtitles: retains preferred languages up to stream limit.
          - Invariants: preserves HDR color metadata across transcode operations.

        Args:
          probe: Media probe extracted via ffprobe.
          preferred_language: Preferred original/audio language code or name.

        Returns:
          RemuxPlan with criteria, target streams, video/audio outputs, and compliance status.
        """
        preferred = normalize_language(preferred_language or "eng")
        policy_signature = self.signature_for(preferred_language)
        criteria: list[Criterion] = []

        # 1. Video stream evaluation
        video_output = VideoOutput(transcoded=False)
        if probe.video:
            primary_video = probe.video[0]
            v_codec = primary_video.codec_name.lower()
            needs_video_transcode = False
            if primary_video.has_dovi:
                needs_video_transcode = True
            elif v_codec not in self.video_copy_codecs:
                needs_video_transcode = True
            elif primary_video.bit_rate is not None:
                bitrate_kbps = primary_video.bit_rate // 1000
                if bitrate_kbps > self.playback_maxrate_kbps:
                    needs_video_transcode = True

            if needs_video_transcode:
                criteria.append(Criterion.VIDEO)
                video_output = VideoOutput(
                    transcoded=True,
                    codec="hevc_nvenc",
                    target_codec_name="hevc",
                    preset=self.nvenc_preset,
                    tune=self.nvenc_tune,
                    profile=self.nvenc_profile,
                    pix_fmt=self.nvenc_pix_fmt,
                    cq=self.nvenc_cq,
                    maxrate_kbps=self.playback_maxrate_kbps,
                    bufsize_kbps=self.playback_maxrate_kbps * 2,
                    spatial_aq=self.nvenc_spatial_aq,
                    temporal_aq=self.nvenc_temporal_aq,
                    aq_strength=self.nvenc_aq_strength,
                    rc_lookahead=self.nvenc_rc_lookahead,
                    b_ref_mode=self.nvenc_b_ref_mode,
                    color_primaries=primary_video.color_primaries,
                    color_transfer=primary_video.color_transfer,
                    color_space=primary_video.color_space,
                    color_range=primary_video.color_range,
                )

        # 2. Audio stream evaluation
        audio = list(probe.audio)
        matching = [stream for stream in audio if stream.language in preferred]
        preferred_match = bool(matching)
        compatible_matching = [stream for stream in matching if self._audio_is_compatible(stream)]
        if compatible_matching:
            redundant = [stream for stream in matching if not self._audio_is_compatible(stream)]
            if redundant:
                redundant_indices = {stream.index for stream in redundant}
                audio = [stream for stream in audio if stream.index not in redundant_indices]
                matching = [stream for stream in matching if stream.index not in redundant_indices]
                criteria.append(Criterion.DUPLICATE_ORIGINAL_AUDIO)

        audio_requires_transcode = {
            stream.index for stream in audio if not self._audio_is_compatible(stream)
        }
        if audio_requires_transcode:
            criteria.append(Criterion.AUDIO)

        if matching and audio and audio[0].language not in preferred:
            criteria.append(Criterion.AUDIO_ORDER)
            audio = matching + [stream for stream in audio if stream.language not in preferred]

        # 3. Subtitles & Stream limit evaluation
        kept_subtitles = list(probe.subtitles)
        removed_subtitles: list[Stream] = []
        if probe.counted_streams > self.stream_limit:
            criteria.append(Criterion.STREAM_LIMIT)
            fixed_count = len(probe.video) + len(audio)
            if fixed_count > self.stream_limit:
                return self._exception_plan(
                    probe,
                    tuple(criteria),
                    policy_signature,
                    preferred,
                    preferred_match,
                    f"video+audio streams ({fixed_count}) exceed limit {self.stream_limit}",
                    video_output=video_output,
                )
            capacity = self.stream_limit - fixed_count
            keep_languages = frozenset().union(
                *(normalize_language(language) for language in self.subtitle_keep_languages)
            )
            eligible = [
                stream
                for stream in probe.subtitles
                if stream.language in keep_languages
            ]
            eligible.sort(key=subtitle_priority)
            kept_subtitles = eligible[:capacity]
            kept_indices = {stream.index for stream in kept_subtitles}
            removed_subtitles = [stream for stream in probe.subtitles if stream.index not in kept_indices]

        if not criteria:
            return RemuxPlan(
                path=probe.path,
                policy_signature=policy_signature,
                criteria=(),
                compliant=True,
                policy_exception=None,
                target_container=None,
                video_indices=tuple(stream.index for stream in probe.video),
                audio=tuple(
                    self._audio_output(stream, position, False, normalize_default=False)
                    for position, stream in enumerate(audio)
                ),
                subtitle_indices=tuple(stream.index for stream in probe.subtitles),
                removed_subtitle_indices=(),
                attachment_indices=tuple(stream.index for stream in probe.attachments),
                input_counted_streams=probe.counted_streams,
                output_counted_streams=probe.counted_streams,
                preferred_languages=tuple(sorted(preferred)),
                preferred_language_match=preferred_match,
                video=video_output,
            )

        audio_outputs = tuple(
            self._audio_output(
                stream,
                position,
                stream.index in audio_requires_transcode,
                normalize_default=True,
            )
            for position, stream in enumerate(audio)
        )
        output_count = len(probe.video) + len(audio_outputs) + len(kept_subtitles)
        return RemuxPlan(
            path=probe.path,
            policy_signature=policy_signature,
            criteria=tuple(criteria),
            compliant=False,
            policy_exception=None,
            target_container="matroska",
            video_indices=tuple(stream.index for stream in probe.video),
            audio=audio_outputs,
            subtitle_indices=tuple(stream.index for stream in kept_subtitles),
            removed_subtitle_indices=tuple(stream.index for stream in removed_subtitles),
            attachment_indices=tuple(stream.index for stream in probe.attachments),
            input_counted_streams=probe.counted_streams,
            output_counted_streams=output_count,
            preferred_languages=tuple(sorted(preferred)),
            preferred_language_match=preferred_match,
            video=video_output,
        )

    def _audio_output(
        self,
        stream: Stream,
        position: int,
        transcode: bool,
        *,
        normalize_default: bool,
    ) -> AudioOutput:
        channels = min(stream.channels, self.audio_max_channels) if transcode and stream.channels else stream.channels
        bitrate = (
            min((channels or self.audio_max_channels) * self.audio_channel_bitrate_kbps, self.audio_max_bitrate_kbps)
            if transcode
            else None
        )
        return AudioOutput(
            input_index=stream.index,
            codec=self.audio_target_codec if transcode else "copy",
            channels=channels,
            bitrate_kbps=bitrate,
            language=stream.language,
            title=stream.title,
            dispositions=stream.disposition.names(default=position == 0)
            if normalize_default
            else stream.disposition.names(),
            transcoded=transcode,
        )

    def _audio_is_compatible(self, stream: Stream) -> bool:
        return (
            stream.codec_name in self.audio_copy_codecs
            and (stream.channels or 0) <= self.audio_max_channels
        )

    def _exception_plan(
        self,
        probe: MediaProbe,
        criteria: tuple[Criterion, ...],
        policy_signature: str,
        preferred: frozenset[str],
        preferred_match: bool,
        reason: str,
        video_output: VideoOutput | None = None,
    ) -> RemuxPlan:
        return RemuxPlan(
            path=probe.path,
            policy_signature=policy_signature,
            criteria=criteria,
            compliant=False,
            policy_exception=reason,
            target_container=None,
            video_indices=tuple(stream.index for stream in probe.video),
            audio=tuple(
                self._audio_output(stream, position, False, normalize_default=False)
                for position, stream in enumerate(probe.audio)
            ),
            subtitle_indices=tuple(stream.index for stream in probe.subtitles),
            removed_subtitle_indices=(),
            attachment_indices=tuple(stream.index for stream in probe.attachments),
            input_counted_streams=probe.counted_streams,
            output_counted_streams=probe.counted_streams,
            preferred_languages=tuple(sorted(preferred)),
            preferred_language_match=preferred_match,
            video=video_output or VideoOutput(),
        )


class NoProcessingRequired(ValueError):
    pass


def build_ffmpeg_argv(plan: RemuxPlan, input_path: Path, output_path: Path) -> list[str]:
    if not plan.needs_processing:
        raise NoProcessingRequired("the plan does not require FFmpeg")

    argv = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "info",
        "-nostdin",
        "-i",
        str(input_path),
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
    ]
    for index in plan.video_indices:
        argv.extend(("-map", f"0:{index}"))
    for audio in plan.audio:
        argv.extend(("-map", f"0:{audio.input_index}"))
    for index in plan.subtitle_indices:
        argv.extend(("-map", f"0:{index}"))
    for index in plan.attachment_indices:
        argv.extend(("-map", f"0:{index}"))

    argv.extend(("-c", "copy"))

    # Video configuration if transcoded
    if plan.video.transcoded:
        argv.extend(("-map_metadata:s:v:0", "-1"))
        argv.extend(("-c:v", plan.video.codec))
        argv.extend(("-preset", plan.video.preset))
        argv.extend(("-tune", plan.video.tune))
        argv.extend(("-profile:v", plan.video.profile))
        argv.extend(("-pix_fmt", plan.video.pix_fmt))
        argv.extend(("-rc", "vbr", "-cq:v", str(plan.video.cq), "-b:v", "0"))
        if plan.video.maxrate_kbps > 0:
            argv.extend(
                (
                    "-maxrate:v",
                    f"{plan.video.maxrate_kbps}k",
                    "-bufsize:v",
                    f"{plan.video.bufsize_kbps}k",
                )
            )
        if plan.video.spatial_aq:
            argv.extend(("-spatial-aq", "1", "-aq-strength", str(plan.video.aq_strength)))
        if plan.video.temporal_aq:
            argv.extend(("-temporal-aq", "1"))
        if plan.video.rc_lookahead > 0:
            argv.extend(("-rc-lookahead", str(plan.video.rc_lookahead)))
        if plan.video.b_ref_mode:
            argv.extend(("-b_ref_mode", plan.video.b_ref_mode))
        if plan.video.color_primaries:
            argv.extend(("-color_primaries", plan.video.color_primaries))
        if plan.video.color_transfer:
            argv.extend(("-color_trc", plan.video.color_transfer))
        if plan.video.color_space:
            argv.extend(("-colorspace", plan.video.color_space))
        if plan.video.color_range:
            argv.extend(("-color_range", plan.video.color_range))

    # Audio configuration
    for output_index, audio in enumerate(plan.audio):
        if audio.transcoded:
            argv.extend((f"-c:a:{output_index}", audio.codec))
            if audio.channels is not None:
                argv.extend((f"-ac:a:{output_index}", str(audio.channels)))
            argv.extend((f"-b:a:{output_index}", f"{audio.bitrate_kbps}k"))
        disposition = "+".join(audio.dispositions) if audio.dispositions else "0"
        argv.extend((f"-disposition:a:{output_index}", disposition))
        argv.extend((f"-metadata:s:a:{output_index}", f"language={audio.language}"))
        if audio.title is not None:
            argv.extend((f"-metadata:s:a:{output_index}", f"title={audio.title}"))

    argv.extend(
        (
            "-max_muxing_queue_size",
            "4096",
            "-progress",
            "pipe:1",
            "-y",
            "-f",
            "matroska",
            str(output_path),
        )
    )
    return argv


def normalize_language(value: str) -> frozenset[str]:
    normalized = value.strip().lower().replace("_", "-")
    return LANGUAGE_ALIASES.get(normalized, frozenset({normalized}))


def subtitle_priority(stream: Stream) -> tuple[int, int]:
    if stream.disposition.forced or stream.disposition.default:
        group = 0
    elif stream.language in PORTUGUESE:
        group = 1
    else:
        group = 2
    return group, stream.index
