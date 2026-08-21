"""External service clients (Radarr, Sonarr, Bazarr, Plex, Telegram) and webhook parsers.

Contract:
  - Responsibility: Integrate with homelab services (Radarr/Sonarr catalog and notifications,
    Bazarr subtitle sync, Plex playback status/refresh, Telegram notifications) and parse
    per-application webhook payloads (`normalize_radarr_webhook`, `normalize_sonarr_webhook`).
  - Invariants:
      * Path mappings safely translate container paths across Arr, Plex, and local volumes.
      * Webhook parsers reject malformed schemas with `ArrWebhookError`.
      * Post-processing handles Plex active playback locks and triggers Bazarr disk scans.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from app.database import Database
from app.engine import SidecarResult, delete_sidecars, rename_sidecars


logger = logging.getLogger(__name__)


class IntegrationError(RuntimeError):
    """Base exception for external service integration errors."""


class ArrClientError(IntegrationError):
    pass


class ArrCommandFailed(ArrClientError):
    pass


class PlexError(IntegrationError):
    pass


class PlexUnavailable(PlexError):
    pass


class PlexPathError(PlexError):
    pass


class ActivePlaybackBlocked(PlexError):
    pass


PlexPathActive = ActivePlaybackBlocked


@dataclass(frozen=True, slots=True)
class ArrIdentity:
    arr_type: str
    media_id: int
    file_id: int
    preferred_language: str | None = None
    date_added: str | None = None


@dataclass(frozen=True, slots=True)
class ArrPathMapper:
    movie_library_root: PurePosixPath = PurePosixPath("/movies")
    series_library_root: PurePosixPath = PurePosixPath("/series")
    radarr_mount_root: PurePosixPath = PurePosixPath("/movies")
    sonarr_mount_root: PurePosixPath = PurePosixPath("/series")

    def to_library(self, path_string: str, arr_type: str) -> str:
        arr_type = arr_type.lower()
        if arr_type == "radarr":
            return self._map(path_string, self.radarr_mount_root, self.movie_library_root)
        if arr_type == "sonarr":
            return self._map(path_string, self.sonarr_mount_root, self.series_library_root)
        raise IntegrationError(f"unknown arr type: {arr_type}")

    def _map(self, raw_path: str, source_root: PurePosixPath, target_root: PurePosixPath) -> str:
        path = PurePosixPath(raw_path)
        if ".." in path.parts:
            raise IntegrationError("path traversal is forbidden")
        if not path.is_relative_to(source_root):
            if not str(path).startswith("/"):
                return str(target_root / path)
            raise IntegrationError(f"path {raw_path} is not under source root {source_root}")
        relative = path.relative_to(source_root)
        return str(target_root / relative)


@dataclass(frozen=True, slots=True)
class PlexPathMapper:
    movie_local: Path = Path("/movies")
    series_local: Path = Path("/series")
    movie_plex: Path = Path("/movies")
    series_plex: Path = Path("/series")
    movie_library_root: PurePosixPath | None = None
    series_library_root: PurePosixPath | None = None

    def __post_init__(self) -> None:
        if self.movie_library_root:
            object.__setattr__(self, "movie_local", Path(str(self.movie_library_root)))
            object.__setattr__(self, "movie_plex", Path(str(self.movie_library_root)))
        if self.series_library_root:
            object.__setattr__(self, "series_local", Path(str(self.series_library_root)))
            object.__setattr__(self, "series_plex", Path(str(self.series_library_root)))

    def local_to_plex(self, local_path: str | Path) -> str:
        path = Path(local_path).resolve()
        movie_local_res = self.movie_local.resolve()
        series_local_res = self.series_local.resolve()
        if movie_local_res in path.parents or path == movie_local_res:
            rel = path.relative_to(movie_local_res)
            return (self.movie_plex / rel).as_posix()
        if series_local_res in path.parents or path == series_local_res:
            rel = path.relative_to(series_local_res)
            return (self.series_plex / rel).as_posix()
        str_path = str(local_path)
        if str_path.startswith("/library/movies/"):
            return str_path.replace("/library/movies", "/movies")
        if str_path.startswith("/library/series/"):
            return str_path.replace("/library/series", "/series")
        raise PlexPathError(f"path '{path}' does not map to any configured Plex root")

    def plex_to_local(self, plex_path: str) -> Path:
        pure = Path(plex_path)
        if self.movie_plex in pure.parents or pure == self.movie_plex:
            rel = pure.relative_to(self.movie_plex)
            return (self.movie_local / rel).resolve()
        if self.series_plex in pure.parents or pure == self.series_plex:
            rel = pure.relative_to(self.series_plex)
            return (self.series_local / rel).resolve()
        raise PlexPathError(f"Plex path '{plex_path}' does not map to local storage roots")


class PlexClient:
    def __init__(
        self,
        base_url: str,
        token: str | None,
        mapper: PlexPathMapper | None = None,
        transport: Any = None,
        timeout_seconds: int = 10,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token or ""
        self.mapper = mapper or PlexPathMapper()
        self.transport = transport
        self.timeout_seconds = timeout_seconds

    def __repr__(self) -> str:
        return f"PlexClient(base_url={self.base_url!r}, token='***')"

    def _get(self, path: str) -> bytes:
        url = f"{self.base_url}{path}"
        headers = {"X-Plex-Token": self._token}
        if self.transport:
            return self.transport.request("GET", url, headers=headers)
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                return resp.read()
        except Exception as error:
            raise PlexUnavailable(f"Plex API GET {path} failed: {error}") from error

    def active_sessions_paths(self) -> set[str]:
        raw = self._get("/status/sessions")
        try:
            root = ET.fromstring(raw)
        except Exception as error:
            raise PlexUnavailable(f"failed to parse Plex sessions XML: {error}") from error

        active_paths: set[str] = set()
        for part in root.findall(".//Part"):
            plex_file = part.attrib.get("file")
            if plex_file:
                active_paths.add(plex_file)
                try:
                    active_paths.add(str(self.mapper.plex_to_local(plex_file)))
                except PlexPathError:
                    pass
        return active_paths

    def assert_path_idle(self, local_path: str | Path) -> None:
        active = self.active_sessions_paths()
        try:
            plex_path = self.mapper.local_to_plex(local_path)
        except PlexPathError:
            plex_path = str(local_path)

        for act in active:
            if act in {str(local_path), plex_path} or Path(act).name == Path(local_path).name:
                raise PlexPathActive(f"file '{local_path}' is currently playing on Plex")

    def refresh_path(self, local_path: str | Path) -> tuple[str, str] | None:
        try:
            plex_path = self.mapper.local_to_plex(local_path)
        except PlexPathError:
            plex_path = str(local_path)

        section_id = self._find_section_id(plex_path)
        if not section_id:
            return None
        folder_path = Path(plex_path).parent.as_posix()
        encoded = urllib.parse.quote_plus(folder_path)
        url = f"/library/sections/{section_id}/refresh?path={encoded}"
        self._get(url)
        return section_id, folder_path

    def _find_section_id(self, plex_path: str) -> str | None:
        raw = self._get("/library/sections")
        try:
            root = ET.fromstring(raw)
        except Exception:
            return "1"
        target = Path(plex_path)
        for directory in root.findall(".//Directory"):
            key = directory.attrib.get("key")
            for location in directory.findall("Location"):
                loc_path = Path(location.attrib.get("path", ""))
                if loc_path in target.parents or loc_path == target.parent or loc_path == target:
                    return str(key)
        return "1"


def read_plex_token(preferences_path: Path) -> str | None:
    if not preferences_path.is_file():
        return None
    try:
        tree = ET.parse(preferences_path)
        root = tree.getroot()
        return root.attrib.get("PlexOnlineToken")
    except Exception:
        return None


class ArrClient:
    def __init__(self, base_url: str, api_key: str, transport: Any = None, timeout_seconds: int = 15) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.transport = transport
        self.timeout_seconds = timeout_seconds

    def _request(self, method: str, path: str, payload: dict | None = None) -> Any:
        url = f"{self.base_url}{path}"
        headers = {"X-Api-Key": self.api_key, "Accept": "application/json"}
        if self.transport:
            return self.transport.request(method, url, headers=headers, body=payload)
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                raw = resp.read()
                return json.loads(raw.decode()) if raw else None
        except Exception as error:
            raise ArrClientError(f"Arr API {method} {path} failed: {error}") from error

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def post(self, path: str, payload: dict) -> Any:
        return self._request("POST", path, payload)

    def put(self, path: str, payload: dict) -> Any:
        return self._request("PUT", path, payload)


class RadarrClient(ArrClient):
    def movies(self) -> list[dict]:
        return self.get("/api/v3/movie") or []

    def movie_file(self, file_id: int) -> dict:
        return self.get(f"/api/v3/moviefile/{file_id}")

    def refresh(self, movie_id: int) -> int:
        resp = self.post("/api/v3/command", {"name": "RefreshMovie", "movieIds": [movie_id]})
        return resp.get("id") if isinstance(resp, dict) else resp

    def rename(self, movie_id: int) -> int:
        resp = self.post("/api/v3/command", {"name": "RenameMovie", "movieIds": [movie_id]})
        return resp.get("id") if isinstance(resp, dict) else resp

    def refresh_movie(self, movie_id: int) -> dict:
        return self.post("/api/v3/command", {"name": "RefreshMovie", "movieId": movie_id})

    def rescan_movie(self, movie_id: int) -> dict:
        return self.post("/api/v3/command", {"name": "RescanMovie", "movieId": movie_id})

    def final_file_path(self, movie_id: int) -> str:
        movie = self.get(f"/api/v3/movie/{movie_id}")
        movie_file = movie.get("movieFile") or {}
        if movie_file.get("path"):
            return str(movie_file["path"])
        if movie_file.get("relativePath") and movie.get("path"):
            return f"{movie['path'].rstrip('/')}/{movie_file['relativePath'].lstrip('/')}"
        return ""

    def command_status(self, command_id: int) -> dict:
        return self.get(f"/api/v3/command/{command_id}")


class SonarrClient(ArrClient):
    def series(self) -> list[dict]:
        return self.get("/api/v3/series") or []

    def episode_files_by_series(self, series_id: int) -> list[dict]:
        return self.get(f"/api/v3/episodefile?seriesId={series_id}") or []

    def episode_file(self, file_id: int) -> dict:
        return self.get(f"/api/v3/episodefile/{file_id}")

    def refresh(self, series_id: int) -> int:
        resp = self.post("/api/v3/command", {"name": "RefreshSeries", "seriesId": series_id})
        return resp.get("id") if isinstance(resp, dict) else resp

    def rename_file(self, series_id: int, file_id: int) -> int:
        resp = self.post("/api/v3/command", {"name": "RenameFiles", "seriesId": series_id, "files": [file_id]})
        return resp.get("id") if isinstance(resp, dict) else resp

    def refresh_series(self, series_id: int) -> dict:
        return self.post("/api/v3/command", {"name": "RefreshSeries", "seriesId": series_id})

    def rescan_series(self, series_id: int) -> dict:
        return self.post("/api/v3/command", {"name": "RescanSeries", "seriesId": series_id})

    def final_file_path(self, series_id: int, file_id: int) -> str:
        ep_file = self.get(f"/api/v3/episodefile/{file_id}") or {}
        if ep_file.get("path"):
            return str(ep_file["path"])
        series = self.get(f"/api/v3/series/{series_id}") or {}
        if ep_file.get("relativePath") and series.get("path"):
            return f"{series['path'].rstrip('/')}/{ep_file['relativePath'].lstrip('/')}"
        return ""

    def command_status(self, command_id: int) -> dict:
        return self.get(f"/api/v3/command/{command_id}")


class CommandWaiter:
    def __init__(
        self,
        timeout_seconds: float = 60.0,
        poll_seconds: float = 1.0,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds
        self.monotonic = monotonic or time.monotonic
        self.sleep = sleep or time.sleep

    def wait(self, client: Any, command_id: int) -> dict[str, Any]:
        deadline = self.monotonic() + self.timeout_seconds
        while True:
            resp = client.command_status(command_id) if hasattr(client, "command_status") else client.get(f"/api/v3/command/{command_id}")
            status = resp.get("status")
            if status == "completed":
                return resp
            if status == "failed":
                message = resp.get("message") or "command failed"
                raise ArrCommandFailed(f"Arr command {command_id} failed: {message}")
            if self.monotonic() > deadline:
                raise ArrCommandFailed(f"Arr command {command_id} timed out")
            self.sleep(self.poll_seconds)


class BazarrClient(ArrClient):
    def scan_disk(self, arr_type: str, media_id: int) -> Any:
        if arr_type.lower() == "radarr":
            return self.sync_radarr()
        return self.sync_sonarr()

    def sync_radarr(self) -> Any:
        return self.post("/api/system/tasks", {"taskid": "update_movies"})

    def sync_sonarr(self) -> Any:
        return self.post("/api/system/tasks", {"taskid": "update_series"})


class ArrCatalog:
    def __init__(self, radarr: RadarrClient | None, sonarr: SonarrClient | None) -> None:
        self.radarr = radarr
        self.sonarr = sonarr

    def snapshot(self) -> dict[str, ArrIdentity]:
        identities: dict[str, ArrIdentity] = {}
        if self.radarr:
            try:
                for movie in self.radarr.movies():
                    movie_file = movie.get("movieFile")
                    if movie_file and movie_file.get("path"):
                        resolved = str(Path(movie_file["path"]).resolve())
                        original_lang = movie.get("originalLanguage", {}).get("name") if isinstance(movie.get("originalLanguage"), dict) else None
                        identities[resolved] = ArrIdentity(
                            arr_type="radarr",
                            media_id=int(movie["id"]),
                            file_id=int(movie_file["id"]),
                            preferred_language=original_lang,
                            date_added=movie_file.get("dateAdded"),
                        )
            except Exception as error:
                logger.warning("Radarr snapshot failed: %s", error)

        if self.sonarr:
            try:
                for show in self.sonarr.series():
                    series_id = int(show["id"])
                    original_lang = show.get("originalLanguage", {}).get("name") if isinstance(show.get("originalLanguage"), dict) else None
                    for ep_file in self.sonarr.episode_files_by_series(series_id):
                        if ep_file.get("path"):
                            resolved = str(Path(ep_file["path"]).resolve())
                            identities[resolved] = ArrIdentity(
                                arr_type="sonarr",
                                media_id=series_id,
                                file_id=int(ep_file["id"]),
                                preferred_language=original_lang,
                                date_added=ep_file.get("dateAdded"),
                            )
            except Exception as error:
                logger.warning("Sonarr snapshot failed: %s", error)

        return identities


@dataclass(frozen=True, slots=True)
class PostprocessResult:
    final_library_path: str
    final_arr_path: str = ""
    sidecars: SidecarResult = field(default_factory=SidecarResult)


class ArrPostProcessor:
    def __init__(
        self,
        radarr: RadarrClient | None = None,
        sonarr: SonarrClient | None = None,
        bazarr: BazarrClient | None = None,
        mapper: ArrPathMapper | None = None,
        database: Database | None = None,
        waiter: Any = None,
    ) -> None:
        self.radarr = radarr
        self.sonarr = sonarr
        self.bazarr = bazarr
        self.mapper = mapper or ArrPathMapper()
        self.database = database
        self.waiter = waiter or CommandWaiter()

    def run(
        self,
        *,
        arr_type: str,
        media_id: int,
        file_id: int,
        promoted_library_path: str,
    ) -> PostprocessResult:
        """Execute postprocessing integration with Sonarr/Radarr and Bazarr.

        Contract:
          - Perform media refresh and rename on the target Arr application.
          - If targeted file rename fails due to an invalid/shifted file_id, attempt
            a broader series/movie refresh to reconcile the updated file_id from disk path.
          - Trigger Bazarr disk scan to ensure subtitle synchronization.
          - Return strongly-typed PostprocessResult containing resolved library paths.
        """
        client = self.radarr if arr_type.lower() == "radarr" else self.sonarr
        final_library_path = promoted_library_path
        final_arr_path = promoted_library_path
        sidecar_res = SidecarResult()

        if client is not None:
            # Step 1: Refresh Arr media entry
            if hasattr(client, "refresh"):
                try:
                    cmd_id = client.refresh(media_id)
                    if self.waiter and cmd_id:
                        self.waiter.wait(client, cmd_id)
                except Exception as error:
                    logger.warning("Arr refresh failed for media_id %s: %s", media_id, error)

            # Step 2: Attempt Rename
            rename_succeeded = False
            if hasattr(client, "rename"):
                try:
                    cmd_id = client.rename(media_id)
                    if self.waiter and cmd_id:
                        self.waiter.wait(client, cmd_id)
                    rename_succeeded = True
                except Exception as error:
                    logger.warning("Arr full rename failed for media_id %s: %s", media_id, error)

            if not rename_succeeded and hasattr(client, "rename_file"):
                try:
                    cmd_id = client.rename_file(media_id, file_id)
                    if self.waiter and cmd_id:
                        self.waiter.wait(client, cmd_id)
                    rename_succeeded = True
                except Exception as error:
                    logger.warning(
                        "Arr targeted file_id %s rename failed for media_id %s: %s. Re-checking disk path...",
                        file_id, media_id, error,
                    )
                    # Fallback: file_id may have shifted after Arr rescan. Check if file is registered under new ID.
                    if hasattr(client, "final_file_path"):
                        arr_path = client.final_file_path(media_id) if arr_type.lower() == "radarr" else client.final_file_path(media_id, file_id)
                        if not arr_path and hasattr(client, "episode_files_by_series"):
                            # Try to find current file_id matching promoted path
                            try:
                                ep_files = client.episode_files_by_series(media_id)
                                for ef in ep_files:
                                    if ef.get("path") and Path(ef["path"]).name == Path(promoted_library_path).name:
                                        new_file_id = int(ef["id"])
                                        logger.info("Relocated updated file_id %s for media_id %s", new_file_id, media_id)
                                        cmd_id = client.rename_file(media_id, new_file_id)
                                        if self.waiter and cmd_id:
                                            self.waiter.wait(client, cmd_id)
                                        rename_succeeded = True
                                        file_id = new_file_id
                                        break
                            except Exception as rel_err:
                                logger.warning("Could not relocate file_id for media_id %s: %s", media_id, rel_err)

            # Step 3: Final path lookup
            if hasattr(client, "final_file_path"):
                try:
                    arr_path = client.final_file_path(media_id) if arr_type.lower() == "radarr" else client.final_file_path(media_id, file_id)
                    if arr_path:
                        final_arr_path = arr_path
                        mapped = self.mapper.to_library(arr_path, arr_type)
                        final_library_path = mapped
                        if final_library_path != promoted_library_path:
                            sidecar_res = rename_sidecars(Path(promoted_library_path), Path(final_library_path))
                except Exception as error:
                    logger.warning("Arr final path lookup failed: %s", error)

        # Step 4: Synchronize Bazarr disk state
        if self.bazarr:
            try:
                if hasattr(self.bazarr, "scan_disk"):
                    self.bazarr.scan_disk(arr_type, media_id)
                elif arr_type.lower() == "radarr":
                    self.bazarr.sync_radarr()
                else:
                    self.bazarr.sync_sonarr()
            except Exception as error:
                logger.warning("Bazarr sync failed: %s", error)

        return PostprocessResult(
            final_library_path=final_library_path,
            final_arr_path=final_arr_path,
            sidecars=sidecar_res,
        )


class OutboxWorker:
    def __init__(
        self,
        database: Database,
        on_import: Any = None,
        on_rename: Any = None,
        on_delete: Any = None,
        evaluate_import: Any = None,
        bazarr: Any = None,
        max_attempts: int = 5,
        retry_backoff_base_seconds: int = 10,
        retry_backoff_multiplier: int = 4,
    ) -> None:
        self.database = database
        self.on_import = on_import or evaluate_import
        self.on_rename = on_rename
        self.on_delete = on_delete
        self.bazarr = bazarr
        self.max_attempts = max_attempts
        self.retry_backoff_base_seconds = retry_backoff_base_seconds
        self.retry_backoff_multiplier = retry_backoff_multiplier

    def run_one(self) -> bool:
        item = self.database.claim_outbox("dispatcher")
        if not item:
            return False
        topic = item.get("topic") or item.get("operation")
        payload = item.get("payload") or {}
        try:
            if topic in {"import", "upgrade", "evaluate_import"} and self.on_import:
                self.on_import(payload)
            elif topic in {"rename", "rename_sidecars"}:
                if self.on_rename:
                    self.on_rename(payload)
                old_p = payload.get("old_path") or payload.get("oldPath")
                new_p = payload.get("new_path") or payload.get("newPath")
                if old_p and new_p:
                    rename_sidecars(Path(old_p), Path(new_p))
            elif topic in {"delete", "delete_sidecars"}:
                if self.on_delete:
                    self.on_delete(payload)
                del_p = payload.get("path")
                if del_p:
                    media = self.database.media_file(str(del_p))
                    if media:
                        active = self.database.active_plan_for_media(int(media["id"]))
                        if active:
                            self.database.cancel_plan(active["id"])
                    delete_sidecars(Path(del_p))
            if "id" in item:
                self.database.complete_outbox(item["id"])
            return True
        except Exception as error:
            given_up = False
            if "id" in item:
                attempts = int(item.get("attempt_count", 1))
                given_up = attempts >= self.max_attempts
                self.database.fail_outbox(
                    item["id"],
                    max_attempts=self.max_attempts,
                    retry_backoff_base_seconds=self.retry_backoff_base_seconds,
                    retry_backoff_multiplier=self.retry_backoff_multiplier,
                    error=str(error),
                )
            if given_up:
                return True
            raise


class ArrWebhookError(IntegrationError):
    pass


def normalize_radarr_webhook(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Convert a native Radarr webhook payload into internal events.

    Contract:
      - 'Test' event: returns empty list.
      - 'Download' event: extracts movie and movieFile, returns single 'import' operation.
      - 'Rename' event: extracts renamedMovieFiles, returns 'rename' operations.
      - 'MovieFileDelete' event: extracts movieFile, returns single 'delete' operation.
      - Any unsupported event type returns empty list.

    Raises:
      ArrWebhookError: If payload schema is missing required 'movie' or file path fields.
    """
    event_type = payload.get("eventType") or ""
    if event_type.lower() == "test":
        return []

    movie = payload.get("movie")
    if not isinstance(movie, dict):
        raise ArrWebhookError("radarr webhook payload missing 'movie'")

    media_id = movie.get("id")
    preferred_language = None
    original_language = movie.get("originalLanguage")
    if isinstance(original_language, dict):
        preferred_language = original_language.get("name")

    events: list[tuple[str, dict[str, Any]]] = []
    if event_type == "Download":
        file_obj = payload.get("movieFile")
        if not isinstance(file_obj, dict):
            raise ArrWebhookError("Download event missing 'movieFile'")
        file_id = file_obj.get("id")
        path = file_obj.get("path")
        if not path:
            raise ArrWebhookError("Download event has no file path")
        is_upgrade = bool(payload.get("isUpgrade"))
        event_id = f"radarr:Download:{media_id}:{file_id}:0:{path}"
        events.append((
            "import",
            {
                "eventId": event_id,
                "arrType": "radarr",
                "eventType": "Download",
                "mediaId": media_id,
                "fileId": file_id,
                "path": path,
                "preferredLanguage": preferred_language,
                "isUpgrade": is_upgrade,
            },
        ))
    elif event_type == "Rename":
        for index, item in enumerate(payload.get("renamedMovieFiles") or []):
            old_path = item.get("previousPath")
            new_path = item.get("path")
            if not old_path or not new_path:
                continue
            event_id = f"radarr:Rename:{media_id}:{index}:{old_path}:{new_path}"
            events.append((
                "rename",
                {
                    "eventId": event_id,
                    "arrType": "radarr",
                    "mediaId": media_id,
                    "fileId": item.get("id") or item.get("movieFileId"),
                    "oldPath": old_path,
                    "newPath": new_path,
                },
            ))
    elif event_type == "MovieFileDelete":
        file_obj = payload.get("movieFile")
        if not isinstance(file_obj, dict):
            raise ArrWebhookError("MovieFileDelete event missing 'movieFile'")
        path = file_obj.get("path")
        if not path:
            raise ArrWebhookError("MovieFileDelete event has no file path")
        file_id = file_obj.get("id")
        event_id = f"radarr:MovieFileDelete:{media_id}:{file_id}:{path}"
        events.append((
            "delete",
            {
                "eventId": event_id,
                "arrType": "radarr",
                "mediaId": media_id,
                "fileId": file_id,
                "path": path,
            },
        ))
    return events


def normalize_sonarr_webhook(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Convert a native Sonarr webhook payload into internal events.

    Contract:
      - 'Test' event: returns empty list.
      - 'Download' event: extracts episodeFile or episodeFiles list, returns 'import' operations.
      - 'Rename' event: extracts renamedEpisodeFiles, returns 'rename' operations.
      - 'EpisodeFileDelete' event: extracts episodeFile, returns single 'delete' operation.
      - Any unsupported event type returns empty list.

    Raises:
      ArrWebhookError: If payload schema is missing required 'series' or file path fields.
    """
    event_type = payload.get("eventType") or ""
    if event_type.lower() == "test":
        return []

    series = payload.get("series")
    if not isinstance(series, dict):
        raise ArrWebhookError("sonarr webhook payload missing 'series'")

    media_id = series.get("id")
    preferred_language = None
    original_language = series.get("originalLanguage")
    if isinstance(original_language, dict):
        preferred_language = original_language.get("name")

    events: list[tuple[str, dict[str, Any]]] = []
    if event_type == "Download":
        file_objs: list[dict[str, Any]] = []
        files = payload.get("episodeFiles")
        if isinstance(files, list):
            file_objs = [f for f in files if isinstance(f, dict)]
        single_file = payload.get("episodeFile")
        if isinstance(single_file, dict) and single_file not in file_objs:
            file_objs.insert(0, single_file)

        if not file_objs:
            raise ArrWebhookError("Download event missing 'episodeFile' or 'episodeFiles'")

        is_upgrade = bool(payload.get("isUpgrade"))
        for index, file_obj in enumerate(file_objs):
            file_id = file_obj.get("id")
            path = file_obj.get("path")
            if not path:
                raise ArrWebhookError("Download event has no file path")
            event_id = f"sonarr:Download:{media_id}:{file_id}:{index}:{path}"
            events.append((
                "import",
                {
                    "eventId": event_id,
                    "arrType": "sonarr",
                    "eventType": "Download",
                    "mediaId": media_id,
                    "fileId": file_id,
                    "path": path,
                    "preferredLanguage": preferred_language,
                    "isUpgrade": is_upgrade,
                },
            ))
    elif event_type == "Rename":
        for index, item in enumerate(payload.get("renamedEpisodeFiles") or []):
            old_path = item.get("previousPath")
            new_path = item.get("path")
            if not old_path or not new_path:
                continue
            event_id = f"sonarr:Rename:{media_id}:{index}:{old_path}:{new_path}"
            events.append((
                "rename",
                {
                    "eventId": event_id,
                    "arrType": "sonarr",
                    "mediaId": media_id,
                    "fileId": item.get("id") or item.get("episodeFileId"),
                    "oldPath": old_path,
                    "newPath": new_path,
                },
            ))
    elif event_type == "EpisodeFileDelete":
        file_obj = payload.get("episodeFile")
        if not isinstance(file_obj, dict):
            raise ArrWebhookError("EpisodeFileDelete event missing 'episodeFile'")
        path = file_obj.get("path")
        if not path:
            raise ArrWebhookError("EpisodeFileDelete event has no file path")
        file_id = file_obj.get("id")
        event_id = f"sonarr:EpisodeFileDelete:{media_id}:{file_id}:{path}"
        events.append((
            "delete",
            {
                "eventId": event_id,
                "arrType": "sonarr",
                "mediaId": media_id,
                "fileId": file_id,
                "path": path,
            },
        ))
    return events


class IntegrationService:
    def __init__(self, database: Database, mapper: ArrPathMapper | None = None) -> None:
        self.database = database
        self.mapper = mapper or ArrPathMapper()

    def accept_import(self, payload: dict[str, Any]) -> dict[str, Any]:
        arr_type = payload.get("arrType") or payload.get("arr_type") or "radarr"
        mapped_path = self.mapper.to_library(payload["path"], arr_type)
        event_id = payload.get("eventId") or payload.get("event_id") or uuid.uuid4().hex
        accepted = self.database.accept_inbound_event(
            event_key=f"import:{event_id}",
            event_type="import",
            payload=payload,
            operations=(
                (
                    "evaluate_import",
                    {**payload, "path": mapped_path},
                    f"import:{event_id}",
                ),
            ),
        )
        return {"accepted": accepted, "path": mapped_path}

    def accept_rename(self, payload: dict[str, Any]) -> dict[str, Any]:
        arr_type = payload.get("arrType") or payload.get("arr_type") or "radarr"
        mapped_old = self.mapper.to_library(payload["oldPath"], arr_type)
        mapped_new = self.mapper.to_library(payload["newPath"], arr_type)
        event_id = payload.get("eventId") or payload.get("event_id") or uuid.uuid4().hex
        accepted = self.database.accept_inbound_event(
            event_key=f"rename:{event_id}",
            event_type="rename",
            payload=payload,
            operations=(
                (
                    "rename_sidecars",
                    {**payload, "old_path": mapped_old, "new_path": mapped_new},
                    f"rename:{event_id}",
                ),
            ),
        )
        return {"accepted": accepted, "oldPath": mapped_old, "newPath": mapped_new}

    def accept_delete(self, payload: dict[str, Any]) -> dict[str, Any]:
        arr_type = payload.get("arrType") or payload.get("arr_type") or "radarr"
        mapped_path = self.mapper.to_library(payload["path"], arr_type)
        migration = self.database.migration_for_path(mapped_path)
        if migration:
            return {"preserveSidecars": True, "migrationPlanId": migration["plan_id"]}
        event_id = payload.get("eventId") or payload.get("event_id") or uuid.uuid4().hex
        accepted = self.database.accept_inbound_event(
            event_key=f"delete:{event_id}",
            event_type="delete",
            payload=payload,
            operations=(
                (
                    "delete_sidecars",
                    {**payload, "path": mapped_path},
                    f"delete:{event_id}",
                ),
            ),
        )
        return {"accepted": accepted, "preserveSidecars": False, "migrationPlanId": None}

    def record_import(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.accept_import(payload)

    def record_rename(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.accept_rename(payload)

    def record_delete(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.accept_delete(payload)


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str | None,
        chat_id: str | None,
        transport: Any = None,
        timeout_seconds: int = 10,
    ) -> None:
        self.bot_token = bot_token or ""
        self.chat_id = chat_id or ""
        self.transport = transport
        self.timeout_seconds = timeout_seconds

    def __repr__(self) -> str:
        return "TelegramNotifier(bot_token='***', chat_id='***')"

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def send(self, html_text: str) -> bool:
        if not self.enabled:
            return False
        max_len = 3890
        bounded = html_text[:max_len] if len(html_text) > max_len else html_text
        text = f"<b>Remux Dispatcher</b>\n{bounded}"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        data = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if self.transport:
            self.transport.post(url, data, headers)
            return True
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout_seconds):
                return True
        except Exception as error:
            logger.warning("Telegram notification failed: %s", error)
            return False
