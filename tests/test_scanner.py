import os
from pathlib import Path

from app.media import FileScanner, StabilityTracker, fingerprint


def test_scanner_is_sorted_and_skips_symlinks_hidden_and_partials(tmp_path: Path) -> None:
    root = tmp_path / "movies"
    root.mkdir()
    (root / "b.mkv").write_bytes(b"b")
    (root / "a.mp4").write_bytes(b"a")
    (root / ".hidden.mkv").write_bytes(b"x")
    (root / "bad.partial.mkv").write_bytes(b"x")
    (root / "subtitle.srt").write_bytes(b"x")
    hidden_dir = root / ".hidden"
    hidden_dir.mkdir()
    (hidden_dir / "inside.mkv").write_bytes(b"x")
    (root / "linked.mkv").symlink_to(root / "b.mkv")
    assert [path.name for path in FileScanner((root,), (".mkv", ".mp4")).discover()] == [
        "a.mp4",
        "b.mkv",
    ]


def test_fingerprint_changes_with_size_or_mtime(tmp_path: Path) -> None:
    path = tmp_path / "video.mkv"
    path.write_bytes(b"one")
    first = fingerprint(path)
    path.write_bytes(b"two-two")
    second = fingerprint(path)
    assert first.digest != second.digest
    assert second.size == 7


def test_stability_tracker_requires_unchanged_interval(tmp_path: Path) -> None:
    now = [100.0]
    path = tmp_path / "video.mkv"
    path.write_bytes(b"one")
    tracker = StabilityTracker(60, clock=lambda: now[0])
    assert not tracker.observe(path)
    now[0] = 159.0
    assert not tracker.observe(path)
    now[0] = 160.0
    assert tracker.observe(path)
    path.write_bytes(b"changed")
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1))
    assert not tracker.observe(path)
