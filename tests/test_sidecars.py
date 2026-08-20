from pathlib import Path

from app.engine import delete_sidecars, rename_sidecars


def test_rename_sidecars_preserves_language_flags_and_idx_sub_pair(tmp_path: Path) -> None:
    old = tmp_path / "Old.mp4"
    new = tmp_path / "New.mkv"
    for name in ("Old.pt-BR.forced.srt", "Old.en.idx", "Old.en.sub"):
        (tmp_path / name).write_text(name)
    result = rename_sidecars(old, new)
    assert {Path(target).name for _, target in result.renamed} == {
        "New.pt-BR.forced.srt", "New.en.idx", "New.en.sub"
    }
    assert not list(tmp_path.glob("Old.*"))


def test_identical_target_is_deduplicated_without_overwrite(tmp_path: Path) -> None:
    old = tmp_path / "Old.mp4"
    new = tmp_path / "New.mkv"
    (tmp_path / "Old.pt.srt").write_text("same")
    (tmp_path / "New.pt.srt").write_text("same")
    result = rename_sidecars(old, new)
    assert result.renamed == ()
    assert result.duplicates_removed == (str(tmp_path / "Old.pt.srt"),)
    assert (tmp_path / "New.pt.srt").read_text() == "same"


def test_different_target_gets_deterministic_collision_name(tmp_path: Path) -> None:
    old = tmp_path / "Old.mp4"
    new = tmp_path / "New.mkv"
    (tmp_path / "Old.pt.srt").write_text("old")
    (tmp_path / "New.pt.srt").write_text("existing")
    result = rename_sidecars(old, new)
    assert len(result.collisions) == 1
    collision = Path(result.collisions[0])
    assert collision.name.startswith("New.collision-")
    assert collision.name.endswith(".pt.srt")
    assert collision.read_text() == "old"
    assert (tmp_path / "New.pt.srt").read_text() == "existing"


def test_delete_sidecars_only_removes_supported_exact_stem(tmp_path: Path) -> None:
    video = tmp_path / "Movie.mkv"
    keep = tmp_path / "Movie trailer.pt.srt"
    remove = tmp_path / "Movie.pt.srt"
    ignored = tmp_path / "Movie.nfo"
    for path in (keep, remove, ignored):
        path.write_text("x")
    assert delete_sidecars(video) == (str(remove),)
    assert keep.exists() and ignored.exists()
