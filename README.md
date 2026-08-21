# Transcoder

Container-first microservice for automated media evaluation, audio normalization (EAC3 5.1), HEVC NVENC transcode/remuxing, HDR preservation, and integration across Radarr, Sonarr, Plex, and Bazarr.

## Development

Requires Python 3.12.

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements-dev.txt
./.venv/bin/pytest
```

Run service locally:

```bash
TRANSCODER_API_KEY=your-api-key ./.venv/bin/python -m app.main
```

## Endpoints

- `GET /api/v1/health` - Healthcheck endpoint.
- `GET /api/v1/status` - Processing status, active window, and queue status.
- `POST /api/v1/webhooks/arr` - Unified webhook for Radarr/Sonarr events (Download, Upgrade, Rename, Delete).
- `POST /api/v1/webhooks/radarr` - Legacy unauthenticated Radarr webhook endpoint.
- `POST /api/v1/webhooks/sonarr` - Legacy unauthenticated Sonarr webhook endpoint.
- `GET /api/v1/media?state=` - List media items filtered by state.
- `GET /api/v1/search?q=` - Search tracked media items.
- `POST /api/v1/manual-runs` - Trigger a manual transcode/remux job.
- `GET /api/v1/manual-runs/{id}` - Query status of a manual job.
- `POST /api/v1/jobs/{id}/cancel` - Cancel a queued or running job.
- `POST /api/v1/reports` - Generate dry-run remux evaluation reports.

*Note: All endpoints require `X-API-Key` authentication header except webhooks.*

## Key Features

- **Media Processing Policy**: Audio track normalization (e.g. EAC3 5.1 downmix), track re-ordering, stream limit enforcement, and HDR/Dolby Vision preservation.
- **FFmpeg Executor**: Non-shell execution with progress reporting, graceful cancellation, and space margin validation.
- **Atomic Promotion**: Safe replacement of original media files with strict validation of container, streams, and duration before final swapping.
- **Ecosystem Integration**: Outbox pattern for idempotent notifications to Radarr, Sonarr, Plex, and Bazarr.

