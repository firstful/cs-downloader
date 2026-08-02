# cs-downloader

Headless, CloudStream-style media download service for the Sutliff homelab.

Emulates the CloudStream `MainAPI` interaction model over HTTP:
`search` → `load` (episodes) → `resolve links` → `enqueue download` → `poll status` → `fetch file`.

- **Metadata:** TMDB (same key the StreamPlay extension uses)
- **Sources:** providers transcribed from the StreamPlay extension (decompiled `StreamPlay.cs3` from the Phisher repo) — 2Embed verified end-to-end; more in the registry
- **Downloads:** yt-dlp (HLS/direct) with per-provider headers/referer
- **Deploy:** Flux (reeds/sutliff-cluster) → k3s workers; `cs-downloader.homelab.internal` (LAN)

## API

| Method | Path | Description |
|---|---|---|
| GET | `/health` | liveness |
| GET | `/api/providers` | provider registry + status |
| GET | `/api/search?q=...&page=1` | TMDB search → CloudStream-style results |
| GET | `/api/load?url=tmdb://155` | media detail + episode list |
| POST | `/api/links` | resolve stream links for a title/episode |
| POST | `/api/downloads` | enqueue a download |
| GET | `/api/downloads` | list jobs |
| GET | `/api/downloads/{id}` | job status + progress |
| GET | `/api/downloads/{id}/file` | download the completed file |
| DELETE | `/api/downloads/{id}` | delete job + file |

## Enqueue example

```bash
# search
curl -s 'http://cs-downloader.homelab.internal/api/search?q=Interstellar' | jq

# resolve links for a movie (tmdb://155 = The Dark Knight)
curl -s -X POST http://cs-downloader.homelab.internal/api/links \
  -H 'Content-Type: application/json' \
  -d '{"tmdb_id": 155, "media_type": "movie", "providers": ["2embed"]}'

# enqueue
curl -s -X POST http://cs-downloader.homelab.internal/api/downloads \
  -H 'Content-Type: application/json' \
  -d '{"tmdb_id": 155, "media_type": "movie", "title": "The Dark Knight (2008)", "provider": "2embed"}'

# poll
curl -s http://cs-downloader.homelab.internal/api/downloads/1 | jq

# fetch
curl -o dark-knight.mp4 http://cs-downloader.homelab.internal/api/downloads/1/file
```

## Provider notes (honest status)

- **2embed** — verified working end-to-end (imdbId → uqloads embed → HLS). Movies + TV.
- **tmdb** — metadata only (search/load/episodes).
- Experimental slots in the registry (Xpass, Peachify, VidFast, vaplayer) exist but their
  backends changed since the extension v658 was built; wired in as they're re-verified.

## Build / deploy

- Image: `192.168.1.2:3000/reeds/cs-downloader:<tag>` (Gitea Actions workflow, or
  manual `docker build` + push on the control plane)
- Manifests: `apps/streamplay/` in `reeds/sutliff-cluster` (Flux)
- Storage: `streamplay-data` (Longhorn, SQLite) + `streamplay-downloads` (nfs-media, files)
