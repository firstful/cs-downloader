# cs-downloader — Provider Backlog

Living backlog for adding/re-verifying download providers. Updated 2026-08-05
after the enc-dec.app discovery (see below). Sources of truth:
- **reeds/cs-downloader** — the service; providers in `app/providers/`
- **phisher98/cloudstream-extensions-phisher** (GitHub) — StreamPlay.cs3 v658 decompiled
  with jadx is the reference implementation for every provider chain
- **smy778/EncDecEndpoints** (GitHub) — official client + README for the `enc-dec.app`
  service; cloned at `/tmp/encdec-endpoints`; samples in `samples/`

## Current registry state (`app/providers/registry.py`)

| Provider | Status | Notes |
|---|---|---|
| tmdb | ok | metadata only |
| 2embed | ok | swish route only; vnest dead |
| hindmoviez | ok | English+Hindi, season packs |
| videasy | ok | seed handshake → enc-dec.app dec-videasy → moon.ironwallnet.net m3u8 |
| xpass | ok | page ships backups[]; server-issued tokens |
| rivestream | experimental | resolver-only; CDN 403/404 from host; out of auto-resolve |
| peachify | experimental | **unblocked by enc-dec.app** |
| vidfast | experimental | **unblocked by enc-dec.app** |
| vaplayer | experimental | streamdata.vaplayer.ru — route discovery still needed |

## enc-dec.app discovery (2026-08-05)

`smy778/EncDecEndpoints` is the service behind the exact decrypt API our videasy
provider already calls (`https://enc-dec.app/api/dec-videasy`). It is **closed-source
by design** (the README: "as the encryption/decryption methods are frequently changing
and have become increasingly difficult to maintain due to patches, the code has been
made closed-source") — so do NOT expect the cipher; treat enc-dec.app as a black-box
decrypt service with a documented API surface and per-site samples.

- Base: `https://enc-dec.app/api`
- **Rate limit: 40 req/s** (higher by request) — shared across ALL sites; our worker
  pool must throttle to stay under this or it will 429 and degrade other providers.
- Status/db pages: https://enc-dec.app/status · https://enc-dec.app/db/
- General pattern: GET encrypted payload from the site → POST `{text, ...}` to the
  site's `dec-*` endpoint → `{"status": 200, "result": {...}}`.

## Backlog (priority order)

### P0 — low effort, sample exists, fills gaps now

1. **peachify** — `samples/peachify.py`
   - Servers: `https://usa.eat-peach.sbs/{air,holly,multi}` + `https://uwu.eat-peach.sbs/{moviebox,net}`
   - Movie `/{path}/movie/{tmdb_id}` · TV `/{path}/tv/{tmdb_id}/{s}/{e}` → `GET` → `["data"]` → `POST /api/dec-peachify {text}`
   - Same shape as videasy — ~30 min port.
2. **vidfast** — `samples/vidfast.py`
   - `https://vidfast.vc/tv/{tmdb}/{s}/{e}/` → regex `"(?:en|token)":"(.*?)"` → `GET /api/enc-vidfast?text=` → `{servers, stream, token}` → POST servers with `X-CSRF-Token: token` → `POST /api/dec-vidfast` (twice: servers, then stream).
   - Multi-step w/ CSRF token — ~1 hr port.

### P1 — medium effort, broad catalog

3. **cinejoy** — `samples/cinejoy.py`; `api.shegu.st` backend; `enc-cinejoy?url=<encoded api url>` then `dec-cinejoy`. ~1 hr.
4. **hexa** — `samples/hexa.py`; `theemoviedb.hexa.su/api` / `flixer.sh`; `enc-hexa` (challenge token) → `dec-hexa`. Also present in StreamPlay as `invokeHexa` (decompiled, clean). ~1 hr.
5. **yflix / 1movies** — `samples/1movies_yflix.py` + `samples/database_flix.py`; yflix.to/1movies.bz/solarmovie.fi ajax endpoints; `enc-movies-flix`/`dec-movies-flix`. **Bonus:** enc-dec.app hosts an auto-updating TMDB/IMDb database (`/db/flix`) that could back a search provider. ~1–2 hrs.

### P2 — medium, more niche

6. **cinesrc** — `samples/challenge/cinesrc.py`; `cinesrc.st/embed`; `enc-cinesrc` (token + next-action headers) → `dec-cinesrc`.
7. **vidcore / vidup** — same enc/dec family as vidfast; samples present.
8. **vidsync** — `samples/vidsync.py`; `enc-vidsync` (token) → `dec-vidsync`.
9. **vidlink** — `samples/vidlink.py`; `enc-vidlink?text=<tmdb_id>` only (no decrypt step? verify).
10. **onetouchtv** — `samples/onetouchtv.py`; `api3.devcorp.me`; `dec-onetouchtv`.

### P3 — niche / anime / hosters

11. **animekai** — `samples/animekai.py`; ajax endpoints + `enc-kai`/`dec-kai`; also `/db/kai` MAL/AniList mapping DB.
12. **kisskh** — `samples/kisskh.py`; DramaList/Episode API; `enc-kisskh` (type=vid/sub) / `dec-kisskh`.
13. **meowtv** — `samples/meowtv.py`; `dec-meowtv`.
14. **megaup / rapidshare / abyss / flixcloud** — hosters; `dec-mega`, `dec-rapid`, `dec-abyss`, `dec-flixcloud` + `parse-flixcloud`. Useful only as last-resort file hosts.

## Process for each provider

1. Read the matching sample in `/tmp/encdec-endpoints/samples/` AND the decompiled
   StreamPlay implementation (`/tmp/sp-out3/sources/com/phisher98/StreamPlayExtractor.java`
   — grep `invoke<Provider>`; re-run jadx with `--show-bad-code --comments-level debug`
   if a method shows "Method not decompiled").
2. Live-verify the site endpoints with curl (`-sk -4`, Chrome UA, correct Origin/Referer).
3. Implement `resolve()` in `app/providers/<name>.py`, register in `registry.py`
   (status `ok` ONLY after a real E2E download; else `experimental`).
4. Build/push image, bump tag in sutliff-cluster, Flux applies.
5. E2E: enqueue → done → ffprobe. Update this backlog.

## Pitfalls / notes

- **enc-dec.app is a shared external dependency** — our videasy provider already depends
  on it (that's the extension's own design; verified 2026-08-05). If it goes down,
  videasy AND every new provider here break together. Consider a health check on
  `https://enc-dec.app/status` in the dash.
- **40 req/s shared cap** — a bulk Peppa-style queue hammering dec-videasy could
  throttle other consumers. Keep per-provider rate limiting in the worker.
- The phisher repo ships `.jar` files for many providers (cleaner decompile than .cs3);
  StreamPlay itself is .cs3 only. Use the jars when mining new chains.
- Reed's stance: StreamPlay is ONE source provider, not the service's identity — new
  sources plug into the registry as first-class providers.
