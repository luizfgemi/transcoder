from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from app.integrations import ArrCommandFailed, CommandWaiter, RadarrClient, SonarrClient


@dataclass
class FakeTransport:
    responses: list[Any]
    calls: list[tuple[str, str, dict[str, Any] | None]] = field(default_factory=list)

    def request(self, method: str, url: str, *, headers: dict[str, str], body=None) -> Any:
        assert headers["X-Api-Key"] == "secret"
        self.calls.append((method, url, body))
        return self.responses.pop(0)


def test_radarr_uses_exact_refresh_and_rename_contracts() -> None:
    transport = FakeTransport([{"id": 10}, {"id": 11}])
    client = RadarrClient("http://radarr:7878", "secret", transport)
    assert client.refresh(42) == 10
    assert client.rename(42) == 11
    assert transport.calls == [
        ("POST", "http://radarr:7878/api/v3/command", {"name": "RefreshMovie", "movieIds": [42]}),
        ("POST", "http://radarr:7878/api/v3/command", {"name": "RenameMovie", "movieIds": [42]}),
    ]


def test_sonarr_renames_only_the_processed_file() -> None:
    transport = FakeTransport([{"id": 7}, {"id": 8}])
    client = SonarrClient("http://sonarr:8989", "secret", transport)
    assert client.refresh(3) == 7
    assert client.rename_file(3, 99) == 8
    assert transport.calls[1][2] == {"name": "RenameFiles", "seriesId": 3, "files": [99]}


def test_command_waiter_completes_without_real_sleep() -> None:
    transport = FakeTransport([{"status": "queued"}, {"status": "completed", "id": 5}])
    client = RadarrClient("http://radarr:7878", "secret", transport)
    ticks = iter([0.0, 0.1])
    waiter = CommandWaiter(timeout_seconds=1, poll_seconds=0, monotonic=lambda: next(ticks), sleep=lambda _: None)
    assert waiter.wait(client, 5)["status"] == "completed"


def test_command_waiter_surfaces_failure_without_secret() -> None:
    transport = FakeTransport([{"status": "failed", "message": "bad media"}])
    client = RadarrClient("http://radarr:7878", "secret", transport)
    with pytest.raises(ArrCommandFailed, match="bad media") as failure:
        CommandWaiter().wait(client, 5)
    assert "secret" not in str(failure.value)


def test_final_paths_handle_relative_arr_payloads() -> None:
    radarr_transport = FakeTransport([{"path": "/movies/Foo", "movieFile": {"relativePath": "Foo.mkv"}}])
    assert RadarrClient("http://radarr", "secret", radarr_transport).final_file_path(1) == "/movies/Foo/Foo.mkv"
    sonarr_transport = FakeTransport([{"relativePath": "Season 01/E01.mkv"}, {"path": "/series/Foo"}])
    assert SonarrClient("http://sonarr", "secret", sonarr_transport).final_file_path(1, 2) == "/series/Foo/Season 01/E01.mkv"
