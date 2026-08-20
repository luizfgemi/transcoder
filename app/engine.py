"""FFmpeg execution, output validation, atomic promotion, and sidecar management.

Contract:
  - Responsibility: Execute encoding pipelines (`ProcessingPipeline`, `FFmpegExecutor`),
    validate remuxed outputs against plan invariants (`validate_output`), and atomically
    promote verified cache files into final library locations (`SafePromoter`).
  - Inputs: `RemuxPlan`, source files, and cache targets.
  - Outputs: Verified destination `.mkv` files and sidecar migration results (`SidecarResult`).
  - Invariants:
      * Atomic promotions must preserve file metadata and rollback on failure.
      * Source modifications during encoding abort promotion (`SourceChanged`).
      * Invariants in `validate_output` enforce container type, duration tolerance, HDR colorimetry, and DOVI absence/presence.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import subprocess
import threading
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from app.media import Fingerprint, MediaProbe, ProbeRunner, fingerprint
from app.policy import Policy, RemuxPlan, build_ffmpeg_argv


logger = logging.getLogger(__name__)


class ExecutionError(RuntimeError):
    """Raised when an external FFmpeg/mkvmerge process fails."""


class ValidationError(ValueError):
    """Raised when encoded output violates duration, stream count, HDR, or DOVI invariants."""


class PromotionError(RuntimeError):
    pass


class InsufficientSpace(PromotionError):
    pass


class SourceChanged(PromotionError):
    pass


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    exit_code: int
    cancelled: bool
    progress: dict[str, str]
    log_tail: tuple[str, ...]
    started_at: str
    finished_at: str


class FFmpegExecutor:
    def __init__(
        self,
        *,
        log_tail_lines: int = 100,
        terminate_timeout_seconds: float = 10.0,
    ) -> None:
        self.log_tail_lines = log_tail_lines
        self.terminate_timeout_seconds = terminate_timeout_seconds

    def run(
        self,
        argv: list[str],
        *,
        cancel_event: threading.Event | None = None,
        on_progress: Callable[[dict[str, str]], None] | None = None,
    ) -> ExecutionResult:
        if not argv or Path(argv[0]).name != "ffmpeg":
            raise ExecutionError("executor accepts only an ffmpeg argv")
        started_at = datetime.now(UTC).isoformat()
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                shell=False,
            )
        except OSError as error:
            raise ExecutionError(f"unable to start ffmpeg: {error}") from error

        progress: dict[str, str] = {}
        pending_progress: dict[str, str] = {}
        log_tail: deque[str] = deque(maxlen=self.log_tail_lines)
        cancelled = False
        finished = threading.Event()

        def watch_cancel() -> None:
            if cancel_event is None:
                return
            cancel_event.wait()
            if not finished.is_set() and process.poll() is None:
                process.terminate()

        watcher = threading.Thread(target=watch_cancel, name="ffmpeg-cancel-watch", daemon=True)
        watcher.start()
        assert process.stdout is not None
        try:
            for raw_line in process.stdout:
                line = raw_line.rstrip("\r\n")
                log_tail.append(line)
                if "=" in line:
                    key, value = line.split("=", 1)
                    if key.replace("_", "").isalnum() and " " not in key:
                        pending_progress[key] = value
                        if key == "progress":
                            progress = dict(pending_progress)
                            pending_progress.clear()
                            if on_progress:
                                on_progress(progress)
                if cancel_event and cancel_event.is_set() and process.poll() is None:
                    cancelled = True
                    process.terminate()
                    break
            if cancelled:
                try:
                    process.wait(timeout=self.terminate_timeout_seconds)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            else:
                process.wait()
            if cancel_event and cancel_event.is_set():
                cancelled = True
        finally:
            finished.set()
            process.stdout.close()

        finished_at = datetime.now(UTC).isoformat()
        result = ExecutionResult(
            exit_code=int(process.returncode),
            cancelled=cancelled,
            progress=progress,
            log_tail=tuple(log_tail),
            started_at=started_at,
            finished_at=finished_at,
        )
        if result.exit_code != 0 and not result.cancelled:
            detail = next((line for line in reversed(result.log_tail) if line.strip()), "no ffmpeg output")
            raise ExecutionError(f"ffmpeg exited with {result.exit_code}: {detail}")
        return result


@dataclass(frozen=True, slots=True)
class ValidationResult:
    valid: bool
    source_duration: float
    output_duration: float
    duration_delta: float
    duration_delta_seconds: float
    expected_streams: int
    output_streams: int


def validate_output(
    source_probe: MediaProbe,
    output_probe: MediaProbe,
    plan: RemuxPlan,
    *,
    duration_tolerance_seconds: float = 2.0,
) -> ValidationResult:
    """Validate that the remuxed/transcoded output file satisfies plan invariants.

    Contract:
      - Target container must be Matroska (.mkv) and output size > 0 bytes.
      - Output duration must match source duration within `duration_tolerance_seconds`.
      - Video, audio, and subtitle stream counts must match the plan.
      - Color transfer, primaries, and space metadata must match the source when present (HDR10 preservation).
      - DOVI contract:
          * If video was transcoded: output MUST NOT contain DOVI configuration side data (DOVI removed).
          * If video was copied: output MUST retain DOVI configuration side data when present in source.

    Raises:
      ValidationError: If any invariant check fails.
    """
    logger.info("validation started: file=%s", Path(source_probe.path).name)
    if not output_probe.format_names or "matroska" not in output_probe.format_names:
        logger.error("validation failed: file=%s reason=target container is not matroska", Path(source_probe.path).name)
        raise ValidationError("target container is not matroska")

    if output_probe.size <= 0:
        logger.error("validation failed: file=%s reason=output file size is 0 bytes", Path(source_probe.path).name)
        raise ValidationError("output file size is 0 bytes")

    source_duration = float(source_probe.duration_seconds or 0.0)
    output_duration = float(output_probe.duration_seconds or 0.0)
    delta = abs(source_duration - output_duration)
    if delta > duration_tolerance_seconds:
        logger.error(
            "validation failed: file=%s reason=duration mismatch (source=%.2fs output=%.2fs delta=%.2fs > %.2fs)",
            Path(source_probe.path).name, source_duration, output_duration, delta, duration_tolerance_seconds,
        )
        raise ValidationError(
            f"duration delta exceeds tolerance ({delta:.3f}s > {duration_tolerance_seconds}s)"
        )

    expected_video_count = len(plan.video_indices)
    if len(output_probe.video) != expected_video_count:
        logger.error(
            "validation failed: file=%s reason=video stream count mismatch (expected=%s got=%s)",
            Path(source_probe.path).name, expected_video_count, len(output_probe.video),
        )
        raise ValidationError(
            f"expected {expected_video_count} video stream(s), got {len(output_probe.video)}"
        )

    if source_probe.video and output_probe.video:
        src_v = source_probe.video[0]
        out_v = output_probe.video[0]
        if src_v.color_transfer and out_v.color_transfer != src_v.color_transfer:
            raise ValidationError("color_transfer changed")
        if src_v.color_primaries and out_v.color_primaries != src_v.color_primaries:
            raise ValidationError("color_primaries changed")
        if src_v.color_space and out_v.color_space != src_v.color_space:
            raise ValidationError("color_space changed")
        if src_v.has_dovi:
            if plan.video.transcoded and out_v.has_dovi:
                raise ValidationError("DOVI configuration record still present")
            if not plan.video.transcoded and not out_v.has_dovi:
                raise ValidationError("DOVI configuration record missing")

    expected_audio_count = len(plan.audio)
    if len(output_probe.audio) != expected_audio_count:
        logger.error(
            "validation failed: file=%s reason=audio stream count mismatch (expected=%s got=%s)",
            Path(source_probe.path).name, expected_audio_count, len(output_probe.audio),
        )
        raise ValidationError(
            f"expected {expected_audio_count} audio stream(s), got {len(output_probe.audio)}"
        )

    expected_subtitle_count = len(plan.subtitle_indices)
    if len(output_probe.subtitles) != expected_subtitle_count:
        logger.error(
            "validation failed: file=%s reason=subtitle stream count mismatch (expected=%s got=%s)",
            Path(source_probe.path).name, expected_subtitle_count, len(output_probe.subtitles),
        )
        raise ValidationError(
            f"expected {expected_subtitle_count} subtitle stream(s), got {len(output_probe.subtitles)}"
        )

    if len(output_probe.attachments) != len(source_probe.attachments):
        raise ValidationError("attachment count mismatch")

    if source_probe.chapter_count and output_probe.chapter_count != source_probe.chapter_count:
        raise ValidationError("chapter count mismatch")

    logger.info("validation passed: file=%s delta=%.2fs streams=%s", Path(source_probe.path).name, delta, output_probe.counted_streams)
    return ValidationResult(
        valid=True,
        source_duration=source_duration,
        output_duration=output_duration,
        duration_delta=delta,
        duration_delta_seconds=delta,
        expected_streams=plan.output_counted_streams,
        output_streams=output_probe.counted_streams,
    )


@dataclass(frozen=True, slots=True)
class PromotionResult:
    plan_id: str
    final_path: str
    original_path: str
    backup_path: str | None
    marker_path: str | None
    migrated_extension: bool
    hardlink_count: int = 1


def copy_buffered(
    source: Path,
    target: Path,
    *,
    buffer_size: int = 16 * 1024 * 1024,
    sync: bool = True,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(source, "rb") as src, open(target, "wb") as dst:
        while chunk := src.read(buffer_size):
            dst.write(chunk)
        dst.flush()
        if sync:
            os.fsync(dst.fileno())
    shutil.copystat(source, target)


def ensure_free_space(
    target_directory: Path,
    required_bytes: int,
    headroom_multiplier: float = 1.1,
    *,
    margin: float | None = None,
) -> None:
    target_directory.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(target_directory)
    mult = (1.0 + margin) if margin is not None else headroom_multiplier
    needed = int(required_bytes * mult)
    if usage.free < needed:
        raise InsufficientSpace(
            f"insufficient free disk space in '{target_directory}': available {usage.free} bytes, required {needed} bytes"
        )


@dataclass(frozen=True, slots=True)
class SidecarResult:
    renamed: tuple[tuple[str, str], ...] = ()
    duplicates_removed: tuple[str, ...] = ()
    collisions: tuple[str, ...] = ()


SIDECAR_EXTENSIONS = frozenset({".srt", ".vtt", ".ass", ".ssa", ".idx", ".sub"})


def rename_sidecars(source_base: Path, target_base: Path) -> SidecarResult:
    directory = source_base.parent
    if not directory.is_dir():
        return SidecarResult()
    prefix = source_base.stem
    target_prefix = target_base.stem
    renamed: list[tuple[str, str]] = []
    duplicates_removed: list[str] = []
    collisions: list[str] = []
    for entry in directory.iterdir():
        if entry.is_file() and (entry.name.startswith(f"{prefix}.") or entry.name == prefix) and entry != source_base:
            suffix = entry.name[len(prefix):]
            if not any(suffix.lower().endswith(ext) for ext in SIDECAR_EXTENSIONS):
                continue
            new_target = directory / f"{target_prefix}{suffix}"
            if new_target.exists():
                if new_target.stat().st_size == entry.stat().st_size and new_target.read_bytes() == entry.read_bytes():
                    entry.unlink()
                    duplicates_removed.append(str(entry))
                    continue
                else:
                    # e.g. New.collision-abcd.pt.srt
                    parts = suffix.split(".", 1)
                    rest = f".{parts[1]}" if len(parts) > 1 else suffix
                    collision_name = f"{target_prefix}.collision-{uuid.uuid4().hex[:8]}{rest}"
                    collision_path = directory / collision_name
                    entry.rename(collision_path)
                    collisions.append(str(collision_path))
                    continue
            entry.rename(new_target)
            renamed.append((str(entry), str(new_target)))
    return SidecarResult(
        renamed=tuple(renamed),
        duplicates_removed=tuple(duplicates_removed),
        collisions=tuple(collisions),
    )


def delete_sidecars(video_path: Path) -> tuple[str, ...]:
    directory = video_path.parent
    if not directory.is_dir():
        return ()
    stem = video_path.stem
    removed: list[str] = []
    for entry in directory.iterdir():
        if entry.is_file() and entry != video_path:
            name = entry.name
            if name.startswith(f"{stem}.") and any(name.lower().endswith(ext) for ext in SIDECAR_EXTENSIONS):
                entry.unlink()
                removed.append(str(entry))
    return tuple(removed)


def complete_extension_migration(result: PromotionResult | Any) -> None:
    backup_path = getattr(result, "backup_path", None)
    marker_path = getattr(result, "marker_path", None)
    target_path = getattr(result, "final_path", None)
    complete_extension_migration_paths(backup_path, marker_path, target_path)


def complete_extension_migration_paths(
    backup_path: str | None,
    marker_path: str | None,
    final_path: str | None = None,
) -> None:
    if backup_path:
        bp = Path(backup_path)
        bp.unlink(missing_ok=True)
        try:
            parent = bp.parent
            if parent.is_dir() and not list(parent.iterdir()):
                parent.rmdir()
            top = parent.parent
            if top.name == ".transcoder-backups" and top.is_dir() and not list(top.iterdir()):
                top.rmdir()
        except OSError:
            pass
    if marker_path:
        Path(marker_path).unlink(missing_ok=True)


def recover_extension_migration(marker_path: Path, probe_runner: ProbeRunner) -> str:
    if not marker_path.is_file():
        return "cleaned"
    try:
        data = marker_path.read_text().splitlines()
        target_path = Path(data[0])
        backup_path = Path(data[1])
    except Exception:
        marker_path.unlink(missing_ok=True)
        return "dropped"

    if target_path.is_file() and target_path.stat().st_size > 0:
        try:
            probe_runner.probe(target_path)
            complete_extension_migration_paths(str(backup_path), str(marker_path), str(target_path))
            return "promoted"
        except Exception:
            pass

    if backup_path.is_file():
        original_name = backup_path.name[:-4] if backup_path.name.endswith(".bak") else backup_path.name
        restored_target = backup_path.parent.parent.parent / original_name if backup_path.parent.parent.name == ".transcoder-backups" else target_path.parent / original_name
        backup_path.rename(restored_target)
    complete_extension_migration_paths(str(backup_path), str(marker_path))
    return "restored"


class SafePromoter:
    def __init__(
        self,
        probe_runner: ProbeRunner,
        *,
        duration_tolerance_seconds: float = 2.0,
        before_promote: Callable[[], None] | None = None,
    ) -> None:
        self.probe_runner = probe_runner
        self.duration_tolerance_seconds = duration_tolerance_seconds
        self.before_promote = before_promote

    def promote(
        self,
        *,
        plan_id: str,
        source_path: Path,
        cache_output: Path,
        expected_source: Fingerprint,
        source_probe: MediaProbe,
        plan: RemuxPlan,
    ) -> PromotionResult:
        logger.info("replacement started: file=%s", source_path.name)
        resolved_source = source_path.resolve(strict=True)
        hardlink_count = resolved_source.stat().st_nlink
        current_fp = fingerprint(resolved_source)
        if current_fp.digest != expected_source.digest or current_fp.size != expected_source.size:
            logger.error("replacement aborted: file=%s modified during processing", source_path.name)
            raise SourceChanged("source file was modified during processing")

        if not cache_output.is_file() or cache_output.stat().st_size <= 0:
            raise PromotionError("cache output missing or empty")

        cache_probe = self.probe_runner.probe(cache_output)
        validate_output(
            source_probe, cache_probe, plan, duration_tolerance_seconds=self.duration_tolerance_seconds
        )

        if self.before_promote:
            self.before_promote()
            current_fp = fingerprint(resolved_source)
            if current_fp.digest != expected_source.digest or current_fp.size != expected_source.size:
                raise SourceChanged("source file was modified during processing")

        target_dir = resolved_source.parent
        target_final = target_dir / f"{resolved_source.stem}.mkv"
        migrating_extension = resolved_source.suffix.lower() != ".mkv"

        if migrating_extension and target_final.exists():
            raise PromotionError(f"target '{target_final}' already exists")

        ensure_free_space(target_dir, cache_output.stat().st_size)

        staged_path = target_dir / f".staged.{uuid.uuid4().hex}.mkv"
        backup_path: Path | None = None
        marker_path: Path | None = None

        try:
            copy_buffered(cache_output, staged_path, sync=True)
            staged_probe = self.probe_runner.probe(staged_path)
            validate_output(
                source_probe, staged_probe, plan, duration_tolerance_seconds=self.duration_tolerance_seconds
            )

            backup_dir = target_dir / ".transcoder-backups" / plan_id
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / f"{resolved_source.name}.bak"
            marker_path = target_dir / f".migration.{plan_id}"
            marker_path.write_text(f"{target_final}\n{backup_path}\n")

            resolved_source.rename(backup_path)
            staged_path.rename(target_final)

            if migrating_extension:
                rename_sidecars(resolved_source.with_suffix(""), target_final.with_suffix(""))

            logger.info("replacement completed: file=%s", target_final.name)
            return PromotionResult(
                plan_id=plan_id,
                final_path=str(target_final),
                original_path=str(resolved_source),
                backup_path=str(backup_path),
                marker_path=str(marker_path),
                migrated_extension=migrating_extension,
                hardlink_count=hardlink_count,
            )
        except Exception:
            staged_path.unlink(missing_ok=True)
            if backup_path and backup_path.is_file() and not target_final.is_file():
                backup_path.rename(resolved_source)
            if marker_path:
                marker_path.unlink(missing_ok=True)
            raise


@dataclass(frozen=True, slots=True)
class PipelineResult:
    plan_id: str
    cache_path: str
    promotion: PromotionResult


class ProcessingPipeline:
    def __init__(
        self,
        *,
        cache_root: Path,
        executor: FFmpegExecutor,
        promoter: SafePromoter,
    ) -> None:
        self.cache_root = cache_root
        self.executor = executor
        self.promoter = promoter

    def process(
        self,
        *,
        plan_id: str,
        source_path: Path,
        expected_source: Fingerprint,
        source_probe: MediaProbe,
        plan: RemuxPlan,
        cancel_event: threading.Event | None = None,
        on_progress: Callable[[dict[str, str]], None] | None = None,
    ) -> PipelineResult:
        self.cache_root.mkdir(parents=True, exist_ok=True)
        cache_file = self.cache_root / f"{plan_id}.mkv"
        argv = build_ffmpeg_argv(plan, source_path, cache_file)

        try:
            self.executor.run(argv, cancel_event=cancel_event, on_progress=on_progress)
            promotion = self.promoter.promote(
                plan_id=plan_id,
                source_path=source_path,
                cache_output=cache_file,
                expected_source=expected_source,
                source_probe=source_probe,
                plan=plan,
            )
            return PipelineResult(plan_id=plan_id, cache_path=str(cache_file), promotion=promotion)
        finally:
            cache_file.unlink(missing_ok=True)
