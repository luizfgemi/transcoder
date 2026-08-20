"""Media inspection, stream parsing, path safety guards, and ffprobe wrappers.

Contract:
  - Responsibility: Safely discover media files (`FileScanner`), validate storage path safety
    (`MediaPathGuard`), and extract structured container/stream/chapter metadata (`FFprobeRunner` -> `MediaProbe`).
  - Inputs: Path references to media files on disk.
  - Outputs: Immutable typed data models (`MediaProbe`, `Stream`, `Disposition`).
  - Invariants: Disallow directory traversal or access outside designated movie/series roots;
    provide accurate codec, bitrate, colorimetry (HDR10/DOVI), and stream disposition properties.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, NamedTuple


logger = logging.getLogger(__name__)


class ProbeError(RuntimeError):
    """Raised when ffprobe execution or payload parsing fails."""


class UnsafeMediaPath(ValueError):
    """Raised when a media file path violates security or root containment boundaries."""


@dataclass(frozen=True, slots=True)
class Disposition:
    default: bool = False
    forced: bool = False
    original: bool = False
    comment: bool = False
    lyrics: bool = False
    karaoke: bool = False
    dub: bool = False
    hearing_impaired: bool = False
    visual_impaired: bool = False
    clean_effects: bool = False
    attached_pic: bool = False
    timed_thumbnails: bool = False
    non_diegetic: bool = False

    def names(self, *, default: bool | None = None) -> tuple[str, ...]:
        active: list[str] = []
        is_default = self.default if default is None else default
        if is_default:
            active.append("default")
        if self.forced:
            active.append("forced")
        if self.original:
            active.append("original")
        if self.comment:
            active.append("comment")
        if self.lyrics:
            active.append("lyrics")
        if self.karaoke:
            active.append("karaoke")
        if self.dub:
            active.append("dub")
        if self.hearing_impaired:
            active.append("hearing_impaired")
        if self.visual_impaired:
            active.append("visual_impaired")
        if self.clean_effects:
            active.append("clean_effects")
        if self.attached_pic:
            active.append("attached_pic")
        if self.timed_thumbnails:
            active.append("timed_thumbnails")
        if self.non_diegetic:
            active.append("non_diegetic")
        return tuple(active)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Disposition":
        if not data:
            return cls()
        return cls(
            default=bool(data.get("default")),
            forced=bool(data.get("forced")),
            original=bool(data.get("original")),
            comment=bool(data.get("comment")),
            lyrics=bool(data.get("lyrics")),
            karaoke=bool(data.get("karaoke")),
            dub=bool(data.get("dub")),
            hearing_impaired=bool(data.get("hearing_impaired")),
            visual_impaired=bool(data.get("visual_impaired")),
            clean_effects=bool(data.get("clean_effects")),
            attached_pic=bool(data.get("attached_pic")),
            timed_thumbnails=bool(data.get("timed_thumbnails")),
            non_diegetic=bool(data.get("non_diegetic")),
        )


@dataclass(frozen=True, slots=True)
class Stream:
    index: int
    codec_type: str
    codec_name: str
    channels: int | None = None
    bit_rate: int | None = None
    language: str = "und"
    title: str | None = None
    disposition: Disposition = Disposition()
    profile: str | None = None
    pixel_format: str | None = None
    color_primaries: str | None = None
    color_transfer: str | None = None
    color_space: str | None = None
    color_range: str | None = None
    codec_tag: str | None = None
    tags: dict[str, str] = field(default_factory=dict)
    metadata: tuple[tuple[str, str], ...] = ()
    side_data_types: tuple[str, ...] = ()

    @property
    def has_dovi(self) -> bool:
        """Indicate whether the stream contains Dolby Vision (DOVI) side data metadata."""
        return any("dovi" in s.lower() for s in self.side_data_types)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "codec_type": self.codec_type,
            "codec_name": self.codec_name,
            "channels": self.channels,
            "bit_rate": self.bit_rate,
            "language": self.language,
            "title": self.title,
            "disposition": {name: True for name in self.disposition.names()},
            "profile": self.profile,
            "pixel_format": self.pixel_format,
            "color_primaries": self.color_primaries,
            "color_transfer": self.color_transfer,
            "color_space": self.color_space,
            "color_range": self.color_range,
            "codec_tag": self.codec_tag,
            "tags": dict(self.tags),
            "metadata": [list(item) for item in self.metadata],
            "side_data_types": list(self.side_data_types),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Stream":
        meta = tuple((str(k), str(v)) for k, v in data.get("metadata", []))
        return cls(
            index=int(data["index"]),
            codec_type=str(data["codec_type"]),
            codec_name=str(data["codec_name"]),
            channels=int(data["channels"]) if data.get("channels") is not None else None,
            bit_rate=int(data["bit_rate"]) if data.get("bit_rate") is not None else None,
            language=str(data.get("language") or "und"),
            title=data.get("title"),
            disposition=Disposition.from_dict(data.get("disposition")),
            profile=data.get("profile"),
            pixel_format=data.get("pixel_format"),
            color_primaries=data.get("color_primaries"),
            color_transfer=data.get("color_transfer"),
            color_space=data.get("color_space"),
            color_range=data.get("color_range"),
            codec_tag=data.get("codec_tag"),
            tags=dict(data.get("tags") or {}),
            metadata=meta,
            side_data_types=tuple(data.get("side_data_types") or ()),
        )


@dataclass(frozen=True, slots=True)
class Chapter:
    id: int
    start_time: float = 0.0
    end_time: float = 0.0
    title: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "title": self.title,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Chapter":
        return cls(
            id=int(data["id"]),
            start_time=float(data.get("start_time") or 0.0),
            end_time=float(data.get("end_time") or 0.0),
            title=data.get("title"),
        )


@dataclass(frozen=True, slots=True)
class MediaProbe:
    path: str
    format_names: tuple[str, ...]
    duration_seconds: float
    size: int
    streams: tuple[Stream, ...]
    chapters: tuple[Chapter, ...] = ()
    chapter_count: int = 0
    format_tags: dict[str, str] = field(default_factory=dict)
    format_metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.chapter_count and self.chapters:
            object.__setattr__(self, "chapter_count", len(self.chapters))

    @property
    def video(self) -> tuple[Stream, ...]:
        return tuple(stream for stream in self.streams if stream.codec_type == "video")

    @property
    def audio(self) -> tuple[Stream, ...]:
        return tuple(stream for stream in self.streams if stream.codec_type == "audio")

    @property
    def subtitles(self) -> tuple[Stream, ...]:
        return tuple(stream for stream in self.streams if stream.codec_type == "subtitle")

    @property
    def attachments(self) -> tuple[Stream, ...]:
        return tuple(stream for stream in self.streams if stream.codec_type == "attachment")

    @property
    def counted_streams(self) -> int:
        return len(self.video) + len(self.audio) + len(self.subtitles)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "format_names": list(self.format_names),
            "duration_seconds": self.duration_seconds,
            "size": self.size,
            "streams": [stream.to_dict() for stream in self.streams],
            "chapters": [chapter.to_dict() for chapter in self.chapters],
            "chapter_count": self.chapter_count,
            "format_tags": dict(self.format_tags),
            "format_metadata": [list(item) for item in self.format_metadata],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MediaProbe":
        meta = tuple((str(k), str(v)) for k, v in data.get("format_metadata", []))
        return cls(
            path=str(data["path"]),
            format_names=tuple(data.get("format_names") or ()),
            duration_seconds=float(data.get("duration_seconds") or 0.0),
            size=int(data.get("size") or 0),
            streams=tuple(Stream.from_dict(item) for item in data.get("streams") or ()),
            chapters=tuple(Chapter.from_dict(item) for item in data.get("chapters") or ()),
            chapter_count=int(data.get("chapter_count") or len(data.get("chapters") or ())),
            format_tags=dict(data.get("format_tags") or {}),
            format_metadata=meta,
        )

    @classmethod
    def from_ffprobe(cls, path: str, payload: dict[str, Any]) -> "MediaProbe":
        format_data = payload.get("format", {})
        raw_duration = format_data.get("duration")
        duration = float(raw_duration) if raw_duration is not None else 0.0
        format_name = format_data.get("format_name", "")
        format_names = tuple(part.strip() for part in format_name.split(",") if part.strip())
        size = int(format_data.get("size") or 0)
        format_tags = {str(k).lower(): str(v) for k, v in (format_data.get("tags") or {}).items()}
        format_metadata = tuple((str(k), str(v)) for k, v in (format_data.get("tags") or {}).items())

        streams: list[Stream] = []
        for raw in payload.get("streams", []):
            tags = {str(k).lower(): str(v) for k, v in (raw.get("tags") or {}).items()}
            raw_meta = tuple((str(k), str(v)) for k, v in (raw.get("tags") or {}).items())
            language = tags.get("language") or tags.get("lang") or "und"
            title = tags.get("title")
            bit_rate_raw = raw.get("bit_rate") or tags.get("bps") or tags.get("bps-eng")
            bit_rate = int(bit_rate_raw) if bit_rate_raw and str(bit_rate_raw).isdigit() else None
            channels = int(raw["channels"]) if "channels" in raw else None
            side_data_types = tuple(
                str(item.get("side_data_type"))
                for item in raw.get("side_data_list", [])
                if item.get("side_data_type")
            )

            streams.append(
                Stream(
                    index=int(raw["index"]),
                    codec_type=str(raw.get("codec_type", "unknown")),
                    codec_name=str(raw.get("codec_name", "unknown")),
                    channels=channels,
                    bit_rate=bit_rate,
                    language=language.lower(),
                    title=title,
                    disposition=Disposition.from_dict(raw.get("disposition")),
                    profile=raw.get("profile"),
                    pixel_format=raw.get("pix_fmt"),
                    color_primaries=raw.get("color_primaries"),
                    color_transfer=raw.get("color_trc"),
                    color_space=raw.get("color_space"),
                    color_range=raw.get("color_range"),
                    codec_tag=raw.get("codec_tag_string") or raw.get("codec_tag"),
                    tags=tags,
                    metadata=raw_meta,
                    side_data_types=side_data_types,
                )
            )

        chapters: list[Chapter] = []
        for raw_chapter in payload.get("chapters", []):
            chap_tags = {str(k).lower(): str(v) for k, v in (raw_chapter.get("tags") or {}).items()}
            chapters.append(
                Chapter(
                    id=int(raw_chapter.get("id", 0)),
                    start_time=float(raw_chapter.get("start_time", 0.0)),
                    end_time=float(raw_chapter.get("end_time", 0.0)),
                    title=chap_tags.get("title"),
                )
            )

        return cls(
            path=str(path),
            format_names=format_names,
            duration_seconds=duration,
            size=size,
            streams=tuple(streams),
            chapters=tuple(chapters),
            chapter_count=len(chapters),
            format_tags=format_tags,
            format_metadata=format_metadata,
        )


class ProbeRunner:
    def probe(self, path: Path) -> MediaProbe:
        raise NotImplementedError


class FFprobeRunner(ProbeRunner):
    def __init__(self, binary_or_timeout: str | int = "ffprobe", timeout_seconds: int = 60) -> None:
        if isinstance(binary_or_timeout, int):
            self.binary = "ffprobe"
            self.timeout_seconds = binary_or_timeout
        else:
            self.binary = str(binary_or_timeout)
            self.timeout_seconds = timeout_seconds

    def probe(self, path: Path) -> MediaProbe:
        resolved = Path(path).resolve(strict=True)
        command = [
            self.binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            "-show_chapters",
            str(resolved),
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout_seconds,
            )
        except (subprocess.TimeoutExpired, OSError) as error:
            raise ProbeError(f"ffprobe execution failed for {resolved}: {error}") from error

        if result.returncode != 0:
            raise ProbeError(f"ffprobe failed ({result.returncode}): {result.stderr.strip()}")

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise ProbeError(f"ffprobe output is invalid JSON for {resolved}") from error

        return MediaProbe.from_ffprobe(str(resolved), payload)


@dataclass(frozen=True, slots=True)
class Fingerprint:
    path: str
    size: int
    mtime_ns: int
    digest: str


def fingerprint(path: Path) -> Fingerprint:
    resolved = path.resolve(strict=True)
    stat = resolved.stat()
    size = stat.st_size
    mtime_ns = stat.st_mtime_ns
    header_size = min(size, 64 * 1024)
    hasher = hashlib.sha256()
    hasher.update(str(size).encode())
    hasher.update(str(mtime_ns).encode())
    with open(resolved, "rb") as handle:
        hasher.update(handle.read(header_size))
        if size > header_size:
            handle.seek(max(0, size - header_size))
            hasher.update(handle.read(header_size))
    return Fingerprint(path=str(resolved), size=size, mtime_ns=mtime_ns, digest=hasher.hexdigest())


class StabilityTracker:
    def __init__(self, stability_seconds: int = 60, clock: Callable[[], float] | None = None) -> None:
        self.stability_seconds = stability_seconds
        self.clock = clock or time.time
        self._observations: dict[str, tuple[int, int, float]] = {}

    def observe(self, path: Path) -> bool:
        resolved = str(path.resolve(strict=True))
        stat = path.stat()
        now = self.clock()
        if resolved not in self._observations:
            self._observations[resolved] = (stat.st_size, stat.st_mtime_ns, now)
            return self.stability_seconds == 0
        size, mtime_ns, first_seen = self._observations[resolved]
        if size != stat.st_size or mtime_ns != stat.st_mtime_ns:
            self._observations[resolved] = (stat.st_size, stat.st_mtime_ns, now)
            return False
        return (now - first_seen) >= self.stability_seconds


class ResolvedPath(NamedTuple):
    library: str
    path: Path


class MediaPathGuard:
    def __init__(
        self,
        movie_root: Path,
        series_root: Path,
        allowed_extensions: Iterable[str] = (".mkv", ".mp4", ".avi", ".m4v", ".ts"),
        cache_root: Path | None = None,
    ) -> None:
        self._roots = {
            "movies": movie_root.resolve(),
            "series": series_root.resolve(),
        }
        self.allowed_extensions = tuple(ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in allowed_extensions)
        self.cache_root = cache_root.resolve() if cache_root else None

    def resolve(self, candidate: str | Path) -> ResolvedPath:
        return self.validate(candidate)

    def validate(self, candidate: str | Path) -> ResolvedPath:
        path = Path(candidate)
        try:
            resolved = path.resolve(strict=True)
        except (FileNotFoundError, RuntimeError) as error:
            raise UnsafeMediaPath(f"path does not exist or cannot be resolved: {candidate}") from error

        if not resolved.is_file() or resolved.is_symlink() or Path(candidate).is_symlink():
            raise UnsafeMediaPath(f"path is not a regular file: {candidate}")

        if resolved.name.startswith((".", "@", "_")) or ".partial." in resolved.name:
            raise UnsafeMediaPath(f"hidden or partial files are rejected: {candidate}")

        if resolved.suffix.lower() not in self.allowed_extensions:
            raise UnsafeMediaPath(f"extension '{resolved.suffix}' is not allowed for processing")

        for library, root in self._roots.items():
            if root in resolved.parents or root == resolved.parent:
                return ResolvedPath(library=library, path=resolved)

        raise UnsafeMediaPath(f"path '{resolved}' is outside allowed media roots")

    def is_safe(self, candidate: str | Path) -> bool:
        try:
            self.validate(candidate)
            return True
        except UnsafeMediaPath:
            return False


class FileScanner:
    def __init__(
        self,
        roots_or_movie_root: Path | Iterable[Path],
        series_root_or_extensions: Path | Iterable[str] = (".mkv", ".mp4", ".avi", ".m4v", ".ts"),
        allowed_extensions: Iterable[str] | None = None,
    ) -> None:
        if isinstance(roots_or_movie_root, (tuple, list, set)):
            self.roots = tuple(Path(root).resolve() for root in roots_or_movie_root)
            exts = series_root_or_extensions if isinstance(series_root_or_extensions, (tuple, list, set)) else (".mkv", ".mp4")
            self.allowed_extensions = tuple(ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in exts)
        else:
            self.roots = (roots_or_movie_root.resolve(), series_root_or_extensions.resolve())  # type: ignore[union-attr]
            exts = allowed_extensions or (".mkv", ".mp4", ".avi", ".m4v", ".ts")
            self.allowed_extensions = tuple(ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in exts)

    def discover(self) -> Iterator[Path]:
        found: list[Path] = []
        for root in self.roots:
            if not root.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                dirnames[:] = [d for d in dirnames if not d.startswith((".", "@", "_"))]
                current_dir = Path(dirpath)
                for filename in filenames:
                    if filename.startswith((".", "@", "_")) or ".partial." in filename:
                        continue
                    candidate = current_dir / filename
                    if candidate.is_symlink():
                        continue
                    if candidate.suffix.lower() in self.allowed_extensions and candidate.is_file():
                        found.append(candidate)
        found.sort(key=lambda p: str(p))
        yield from found
