from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.integrations import PlexClient, PlexPathActive, PlexUnavailable, read_plex_token


@dataclass
class FakeXmlTransport:
    responses: list[bytes]
    calls: list[str] = field(default_factory=list)

    def request(self, method: str, url: str, *, headers: dict[str, str]) -> bytes:
        assert headers["X-Plex-Token"] == "secret"
        self.calls.append(url)
        return self.responses.pop(0)


def test_token_is_read_without_being_exposed_in_client_repr(tmp_path: Path) -> None:
    preferences = tmp_path / "Preferences.xml"
    preferences.write_text('<Preferences PlexOnlineToken="secret"/>')
    token = read_plex_token(preferences)
    client = PlexClient("http://plex:32400", token, transport=FakeXmlTransport([]))
    assert "secret" not in repr(client)


def test_activity_guard_matches_exact_promoted_file() -> None:
    xml = b'<MediaContainer><Video><Media><Part file="/movies/A/A.mkv"/></Media></Video></MediaContainer>'
    client = PlexClient("http://plex:32400", "secret", transport=FakeXmlTransport([xml]))
    with pytest.raises(PlexPathActive):
        client.assert_path_idle("/library/movies/A/A.mkv")


def test_invalid_sessions_response_fails_closed() -> None:
    client = PlexClient("http://plex:32400", "secret", transport=FakeXmlTransport([b"not xml"]))
    with pytest.raises(PlexUnavailable):
        client.assert_path_idle("/library/movies/A/A.mkv")


def test_refresh_selects_section_and_targets_parent_directory() -> None:
    sections = b'''<MediaContainer>
      <Directory key="1"><Location path="/movies"/></Directory>
      <Directory key="2"><Location path="/series"/></Directory>
    </MediaContainer>'''
    transport = FakeXmlTransport([sections, b""])
    client = PlexClient("http://plex:32400", "secret", transport=transport)
    assert client.refresh_path("/library/series/Show/Season 01/E01.mkv") == ("2", "/series/Show/Season 01")
    assert transport.calls[-1].endswith("library/sections/2/refresh?path=%2Fseries%2FShow%2FSeason+01")
