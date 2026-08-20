import os
from dataclasses import replace
from pathlib import Path

import pytest

from app.policy import Policy
from app.media import Disposition, MediaProbe, Stream
from app.engine import (
    InsufficientSpace,
    PromotionError,
    SafePromoter,
    SourceChanged,
    complete_extension_migration,
    complete_extension_migration_paths,
    ensure_free_space,
    recover_extension_migration,
)
from app.media import fingerprint


def probes(source_path: Path):
    source = MediaProbe(
        path=str(source_path),
        format_names=("matroska",),
        duration_seconds=10,
        size=3,
        streams=(
            Stream(index=0, codec_type="video", codec_name="hevc"),
            Stream(
                index=1,
                codec_type="audio",
                codec_name="dts",
                channels=6,
                language="eng",
                disposition=Disposition(default=True),
            ),
        ),
    )
    output = MediaProbe(
        path="output",
        format_names=("matroska",),
        duration_seconds=10,
        size=3,
        streams=(
            Stream(index=0, codec_type="video", codec_name="hevc"),
            Stream(
                index=1,
                codec_type="audio",
                codec_name="eac3",
                channels=6,
                language="eng",
                disposition=Disposition(default=True),
            ),
        ),
    )
    return source, output, Policy().evaluate(source, "English")


class ContentProbe:
    def __init__(self, source: MediaProbe, output: MediaProbe) -> None:
        self.source = source
        self.output = output

    def probe(self, path: Path) -> MediaProbe:
        return replace(self.output if path.read_bytes() == b"new" else self.source, path=str(path), size=path.stat().st_size)


def test_promotes_mkv_atomically_and_preserves_hardlink_source(tmp_path: Path) -> None:
    source_path = tmp_path / "movie.mkv"
    torrent_path = tmp_path / "torrent.mkv"
    cache = tmp_path / "cache.mkv"
    source_path.write_bytes(b"old")
    os.link(source_path, torrent_path)
    cache.write_bytes(b"new")
    source, output, plan = probes(source_path)
    result = SafePromoter(ContentProbe(source, output)).promote(
        plan_id="p1",
        source_path=source_path,
        cache_output=cache,
        expected_source=fingerprint(source_path),
        source_probe=source,
        plan=plan,
    )
    assert source_path.read_bytes() == b"new"
    assert torrent_path.read_bytes() == b"old"
    assert result.hardlink_count == 2
    assert Path(result.backup_path).read_bytes() == b"old"
    assert Path(result.marker_path).exists()
    complete_extension_migration(result)
    assert not Path(result.backup_path).exists()
    assert not Path(result.marker_path).exists()


def test_arr_style_output_rename_cannot_move_backup_from_hidden_directory(tmp_path: Path) -> None:
    source_path = tmp_path / "movie-old.mkv"
    cache = tmp_path / "cache.mkv"
    source_path.write_bytes(b"old")
    cache.write_bytes(b"new")
    source, output, plan = probes(source_path)
    result = SafePromoter(ContentProbe(source, output)).promote(
        plan_id="arr-rename", source_path=source_path, cache_output=cache,
        expected_source=fingerprint(source_path), source_probe=source, plan=plan,
    )
    renamed = tmp_path / "movie-new.mkv"
    os.replace(source_path, renamed)
    assert Path(result.backup_path).parent.name == "arr-rename"
    assert Path(result.backup_path).read_bytes() == b"old"
    complete_extension_migration_paths(
        backup_path=result.backup_path, marker_path=result.marker_path, final_path=str(renamed)
    )
    assert not Path(result.backup_path).exists()
    assert not (tmp_path / ".transcoder-backups").exists()
    assert not list(tmp_path.glob("*.partial"))


def test_changed_source_before_final_guard_is_not_replaced(tmp_path: Path) -> None:
    source_path = tmp_path / "movie.mkv"
    cache = tmp_path / "cache.mkv"
    source_path.write_bytes(b"old")
    cache.write_bytes(b"new")
    source, output, plan = probes(source_path)

    def mutate() -> None:
        source_path.write_bytes(b"changed")

    with pytest.raises(SourceChanged):
        SafePromoter(ContentProbe(source, output), before_promote=mutate).promote(
            plan_id="p2",
            source_path=source_path,
            cache_output=cache,
            expected_source=fingerprint(source_path),
            source_probe=source,
            plan=plan,
        )
    assert source_path.read_bytes() == b"changed"


def test_extension_migration_keeps_backup_until_completed(tmp_path: Path) -> None:
    source_path = tmp_path / "movie.mp4"
    cache = tmp_path / "cache.mkv"
    source_path.write_bytes(b"old")
    cache.write_bytes(b"new")
    source, output, plan = probes(source_path)
    probe_runner = ContentProbe(source, output)
    result = SafePromoter(probe_runner).promote(
        plan_id="p3",
        source_path=source_path,
        cache_output=cache,
        expected_source=fingerprint(source_path),
        source_probe=source,
        plan=plan,
    )
    assert not source_path.exists()
    assert Path(result.final_path).read_bytes() == b"new"
    assert Path(result.backup_path).read_bytes() == b"old"
    assert recover_extension_migration(Path(result.marker_path), probe_runner) == "promoted"
    complete_extension_migration(result)
    assert not Path(result.backup_path).exists()
    assert not Path(result.marker_path).exists()


def test_recovery_restores_original_when_target_never_promoted(tmp_path: Path) -> None:
    source_path = tmp_path / "movie.mp4"
    cache = tmp_path / "cache.mkv"
    source_path.write_bytes(b"old")
    cache.write_bytes(b"new")
    source, output, plan = probes(source_path)
    probe_runner = ContentProbe(source, output)
    result = SafePromoter(probe_runner).promote(
        plan_id="p4",
        source_path=source_path,
        cache_output=cache,
        expected_source=fingerprint(source_path),
        source_probe=source,
        plan=plan,
    )
    Path(result.final_path).unlink()
    assert recover_extension_migration(Path(result.marker_path), probe_runner) == "restored"
    assert source_path.read_bytes() == b"old"


def test_collision_and_invalid_output_preserve_source(tmp_path: Path) -> None:
    source_path = tmp_path / "movie.mp4"
    cache = tmp_path / "cache.mkv"
    target = tmp_path / "movie.mkv"
    source_path.write_bytes(b"old")
    cache.write_bytes(b"new")
    target.write_bytes(b"existing")
    source, output, plan = probes(source_path)
    with pytest.raises(PromotionError, match="already exists"):
        SafePromoter(ContentProbe(source, output)).promote(
            plan_id="p5",
            source_path=source_path,
            cache_output=cache,
            expected_source=fingerprint(source_path),
            source_probe=source,
            plan=plan,
        )
    assert source_path.read_bytes() == b"old"
    assert target.read_bytes() == b"existing"


def test_space_check_includes_margin(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.engine.shutil.disk_usage",
        lambda path: type("Usage", (), {"free": 109})(),
    )
    with pytest.raises(InsufficientSpace):
        ensure_free_space(tmp_path, 100, margin=0.10)
