import pytest

from app.integrations import (
    ArrWebhookError,
    normalize_radarr_webhook,
    normalize_sonarr_webhook,
)


# ==========================================
# Radarr Tests
# ==========================================


def radarr_download_payload(is_upgrade: bool = False) -> dict[str, object]:
    return {
        "eventType": "Download",
        "movie": {"id": 10, "title": "Inception", "originalLanguage": {"name": "English"}},
        "movieFile": {"id": 20, "path": "/movies/Inception (2010)/Inception.mkv"},
        "isUpgrade": is_upgrade,
        "downloadClient": "qBittorrent",
    }


def test_radarr_download_becomes_single_import() -> None:
    events = normalize_radarr_webhook(radarr_download_payload(is_upgrade=False))
    assert len(events) == 1
    operation, event = events[0]
    assert operation == "import"
    assert event["arrType"] == "radarr"
    assert event["eventType"] == "Download"
    assert event["mediaId"] == 10
    assert event["fileId"] == 20
    assert event["path"] == "/movies/Inception (2010)/Inception.mkv"
    assert event["preferredLanguage"] == "English"
    assert event["isUpgrade"] is False


def test_radarr_download_with_upgrade_flag() -> None:
    events = normalize_radarr_webhook(radarr_download_payload(is_upgrade=True))
    assert len(events) == 1
    operation, event = events[0]
    assert operation == "import"
    assert event["isUpgrade"] is True


def test_radarr_rename_extracts_renamed_movie_files() -> None:
    payload = {
        "eventType": "Rename",
        "movie": {"id": 10, "title": "Inception"},
        "renamedMovieFiles": [
            {
                "id": 20,
                "previousPath": "/movies/Inception/old.mkv",
                "path": "/movies/Inception/new.mkv",
            }
        ],
    }
    events = normalize_radarr_webhook(payload)
    assert len(events) == 1
    operation, event = events[0]
    assert operation == "rename"
    assert event["arrType"] == "radarr"
    assert event["mediaId"] == 10
    assert event["fileId"] == 20
    assert event["oldPath"] == "/movies/Inception/old.mkv"
    assert event["newPath"] == "/movies/Inception/new.mkv"


def test_radarr_movie_file_delete_becomes_delete() -> None:
    payload = {
        "eventType": "MovieFileDelete",
        "movie": {"id": 10},
        "movieFile": {"id": 20, "path": "/movies/Inception/Inception.mkv"},
    }
    events = normalize_radarr_webhook(payload)
    assert len(events) == 1
    operation, event = events[0]
    assert operation == "delete"
    assert event["arrType"] == "radarr"
    assert event["mediaId"] == 10
    assert event["fileId"] == 20
    assert event["path"] == "/movies/Inception/Inception.mkv"


def test_radarr_test_event_yields_empty_list() -> None:
    assert normalize_radarr_webhook({"eventType": "Test"}) == []


def test_radarr_missing_movie_raises_error() -> None:
    with pytest.raises(ArrWebhookError, match="radarr webhook payload missing 'movie'"):
        normalize_radarr_webhook({"eventType": "Download", "movieFile": {"path": "/a.mkv"}})


def test_radarr_download_missing_movie_file_raises_error() -> None:
    with pytest.raises(ArrWebhookError, match="Download event missing 'movieFile'"):
        normalize_radarr_webhook({"eventType": "Download", "movie": {"id": 1}})


def test_radarr_download_missing_path_raises_error() -> None:
    with pytest.raises(ArrWebhookError, match="Download event has no file path"):
        normalize_radarr_webhook({"eventType": "Download", "movie": {"id": 1}, "movieFile": {"id": 2}})


# ==========================================
# Sonarr Tests
# ==========================================


def test_sonarr_download_single_episode_file() -> None:
    payload = {
        "eventType": "Download",
        "series": {"id": 3, "title": "Breaking Bad", "originalLanguage": {"name": "English"}},
        "episodeFile": {"id": 40, "path": "/series/Breaking Bad/S01E01.mkv"},
        "isUpgrade": False,
    }
    events = normalize_sonarr_webhook(payload)
    assert len(events) == 1
    operation, event = events[0]
    assert operation == "import"
    assert event["arrType"] == "sonarr"
    assert event["eventType"] == "Download"
    assert event["mediaId"] == 3
    assert event["fileId"] == 40
    assert event["path"] == "/series/Breaking Bad/S01E01.mkv"
    assert event["preferredLanguage"] == "English"
    assert event["isUpgrade"] is False


def test_sonarr_download_batch_episode_files() -> None:
    payload = {
        "eventType": "Download",
        "series": {"id": 3, "title": "Breaking Bad", "originalLanguage": {"name": "English"}},
        "episodeFiles": [
            {"id": 41, "path": "/series/Breaking Bad/S01E01.mkv"},
            {"id": 42, "path": "/series/Breaking Bad/S01E02.mkv"},
        ],
        "isUpgrade": True,
    }
    events = normalize_sonarr_webhook(payload)
    assert len(events) == 2
    assert [ev["path"] for _, ev in events] == [
        "/series/Breaking Bad/S01E01.mkv",
        "/series/Breaking Bad/S01E02.mkv",
    ]
    assert all(ev["arrType"] == "sonarr" for _, ev in events)
    assert all(ev["isUpgrade"] is True for _, ev in events)


def test_sonarr_rename_extracts_renamed_episode_files() -> None:
    payload = {
        "eventType": "Rename",
        "series": {"id": 3, "originalLanguage": {"name": "Japanese"}},
        "renamedEpisodeFiles": [
            {"id": 51, "previousPath": "/series/Show/Old1.mkv", "path": "/series/Show/New1.mkv"},
            {"id": 52, "previousPath": "/series/Show/Old2.mkv", "path": "/series/Show/New2.mkv"},
        ],
    }
    events = normalize_sonarr_webhook(payload)
    assert [(op, ev["oldPath"], ev["newPath"]) for op, ev in events] == [
        ("rename", "/series/Show/Old1.mkv", "/series/Show/New1.mkv"),
        ("rename", "/series/Show/Old2.mkv", "/series/Show/New2.mkv"),
    ]
    assert all(ev["arrType"] == "sonarr" for _, ev in events)


def test_sonarr_episode_file_delete_becomes_delete() -> None:
    payload = {
        "eventType": "EpisodeFileDelete",
        "series": {"id": 3},
        "episodeFile": {"id": 40, "path": "/series/Show/S01E01.mkv"},
    }
    events = normalize_sonarr_webhook(payload)
    assert len(events) == 1
    operation, event = events[0]
    assert operation == "delete"
    assert event["arrType"] == "sonarr"
    assert event["mediaId"] == 3
    assert event["fileId"] == 40
    assert event["path"] == "/series/Show/S01E01.mkv"


def test_sonarr_test_event_yields_empty_list() -> None:
    assert normalize_sonarr_webhook({"eventType": "Test"}) == []


def test_sonarr_missing_series_raises_error() -> None:
    with pytest.raises(ArrWebhookError, match="sonarr webhook payload missing 'series'"):
        normalize_sonarr_webhook({"eventType": "Download", "episodeFile": {"path": "/a.mkv"}})


def test_sonarr_download_missing_episode_file_raises_error() -> None:
    with pytest.raises(ArrWebhookError, match="Download event missing 'episodeFile' or 'episodeFiles'"):
        normalize_sonarr_webhook({"eventType": "Download", "series": {"id": 1}})