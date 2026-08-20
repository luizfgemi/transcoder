from pathlib import Path

import pytest

from app.media import MediaPathGuard, UnsafeMediaPath


def guard(tmp_path: Path) -> MediaPathGuard:
    movies = tmp_path / "movies"
    series = tmp_path / "series"
    cache = tmp_path / "cache"
    movies.mkdir()
    series.mkdir()
    cache.mkdir()
    return MediaPathGuard(movies, series, (".mkv", ".mp4"), cache)


def test_accepts_regular_video_inside_root(tmp_path: Path) -> None:
    path_guard = guard(tmp_path)
    movie = tmp_path / "movies" / "Film" / "film.mkv"
    movie.parent.mkdir()
    movie.write_bytes(b"test")
    resolved = path_guard.resolve(movie)
    assert resolved.library == "movies"
    assert resolved.path == movie.resolve()


@pytest.mark.parametrize("name", ["video.srt", ".hidden.mkv", "video.partial.mkv"])
def test_rejects_unsupported_hidden_or_partial(tmp_path: Path, name: str) -> None:
    path_guard = guard(tmp_path)
    path = tmp_path / "movies" / name
    path.write_bytes(b"test")
    with pytest.raises(UnsafeMediaPath):
        path_guard.resolve(path)


def test_rejects_outside_and_symlink(tmp_path: Path) -> None:
    path_guard = guard(tmp_path)
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"test")
    with pytest.raises(UnsafeMediaPath):
        path_guard.resolve(outside)

    linked = tmp_path / "movies" / "linked.mkv"
    linked.symlink_to(outside)
    with pytest.raises(UnsafeMediaPath):
        path_guard.resolve(linked)

