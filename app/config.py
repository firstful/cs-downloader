"""Configuration for streamplay-service (env-driven)."""
import os

# Directories
DATA_DIR = os.environ.get("SP_DATA_DIR", "/data")          # SQLite + misc state
DOWNLOAD_DIR = os.environ.get("SP_DOWNLOAD_DIR", "/downloads")  # completed files
CACHE_DIR = os.environ.get("SP_CACHE_DIR", "/data/cache")   # http cache

# TMDB (same key the StreamPlay extension embeds)
TMDB_API_KEY = os.environ.get("SP_TMDB_API_KEY", "98ae14df2b8d8f8f8136499daf79f0e0")
TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMG = "https://image.tmdb.org/t/p/w500"
TMDB_IMG_ORIG = "https://image.tmdb.org/t/p/original"

# HTTP
USER_AGENT = os.environ.get(
    "SP_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64; rv:139.0) Gecko/20100101 Firefox/139.0",
)
REQUEST_TIMEOUT = int(os.environ.get("SP_HTTP_TIMEOUT", "25"))

# Worker
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("SP_MAX_CONCURRENT", "4"))
MIN_CONCURRENT_DOWNLOADS = int(os.environ.get("SP_MIN_CONCURRENT", "1"))
MAX_RETRIES = int(os.environ.get("SP_MAX_RETRIES", "2"))

# Adaptive concurrency: the controller compares the effective download rate
# against the rate-cap. Below ADAPTIVE_LOW utilization it slowly adds workers
# (up to MAX); above ADAPTIVE_HIGH (or when the queue drains) it sheds back
# toward MIN. ADAPTIVE_SAMPLE_SECONDS is the short window used to measure the
# current rate (the cap window itself is 600s — too slow to react).
ADAPTIVE_INTERVAL = int(os.environ.get("SP_ADAPTIVE_INTERVAL", "60"))        # controller cadence
ADAPTIVE_SAMPLE_SECONDS = int(os.environ.get("SP_ADAPTIVE_SAMPLE_SECONDS", "30"))
ADAPTIVE_LOW = float(os.environ.get("SP_ADAPTIVE_LOW", "0.5"))               # <50% of cap → grow
ADAPTIVE_HIGH = float(os.environ.get("SP_ADAPTIVE_HIGH", "0.85"))            # >85% of cap → shrink

# Rate limiting: max bytes per rolling window (default 5GiB / 10 min)
# Reed 2026-08-04: halved from 10GiB to be conservative. The window is
# enforced by a shared start-gate + per-job fair-share throttle, so a
# parallel worker pool can fill the pipe without exceeding it.
RATE_LIMIT_BYTES = int(os.environ.get("SP_RATE_LIMIT_BYTES", str(5 * 1024 ** 3)))
RATE_WINDOW_SECONDS = int(os.environ.get("SP_RATE_WINDOW_SECONDS", "600"))

# Third-party decrypt/seed services (enc-dec.app + videasy speedracelight seed).
# enc-dec.app is CLOSED-SOURCE with a generous 40 req/s cap (Reed 2026-08-05: "we
# can use it as a dep — just stay well below that"). ALSO covers the videasy seed
# API which Cloudflare rate-limits per-IP (~1015 after rapid bursts). Default 8/s =
# 5x margin under enc-dec.app's cap; tune via SP_ENC_DEC_MAX_RPS if ever needed.
ENC_DEC_MAX_RPS = float(os.environ.get("SP_ENC_DEC_MAX_RPS", "8"))

# Reconciler (wanted-library): sweep cadence + retry backoff for failed attempts
RECONCILE_INTERVAL = int(os.environ.get("SP_RECONCILE_INTERVAL", "900"))   # seconds between sweeps
RETRY_SECONDS = int(os.environ.get("SP_RETRY_SECONDS", "14400"))           # 4h default retry window
# Bulk episode adds (whole seasons) create many wanted rows at once; launch all
# due jobs per sweep and let the worker pool serialize actual downloads.
MAX_RECONCILE_PER_SWEEP = int(os.environ.get("SP_MAX_RECONCILE_PER_SWEEP", "200"))

# yt-dlp
YTDLP_BIN = os.environ.get("SP_YTDLP_BIN", "yt-dlp")       # path or python module
FFMPEG_BIN = os.environ.get("SP_FFMPEG_BIN", "ffmpeg")

# Provider enable/disable (comma separated ids, default all enabled)
DISABLED_PROVIDERS = {
    p.strip() for p in os.environ.get("SP_DISABLED_PROVIDERS", "").split(",") if p.strip()
}

# Post-download validation filters (comma separated ids; empty disables all).
# Filters run in the worker after download+remux and before the file is moved
# into the flat downloads dir; a failing filter rejects the file and the worker
# tries the next link.
FILTERS = {
    p.strip() for p in os.environ.get("SP_FILTERS", "aspect_ratio,duration").split(",") if p.strip()
}

# aspect_ratio filter tuning
AR_TOLERANCE = float(os.environ.get("SP_AR_TOLERANCE", "0.08"))        # relative diff allowed (0.08 = 8%)
AR_SAMPLES = int(os.environ.get("SP_AR_SAMPLES", "12"))                # cropdetect frames sampled
AR_SKIP_START = float(os.environ.get("SP_AR_SKIP_START", "0.03"))      # skip first 3% of duration
AR_SKIP_END = float(os.environ.get("SP_AR_SKIP_END", "0.05"))          # skip last 5% (credits/end cards)
AR_MIN_CROP_FRACTION = float(os.environ.get("SP_AR_MIN_CROP_FRACTION", "0.5"))  # crop must be >= 50% of frame in each dim to count
AR_TV_DEFAULT = float(os.environ.get("SP_AR_TV_DEFAULT", "1.777778"))  # 16:9 strict expected for TV
AR_MOVIE_FALLBACK = float(os.environ.get("SP_AR_MOVIE_FALLBACK", "1.777778"))  # strict expected when a movie has no explicit value
AR_MODE_TV = os.environ.get("SP_AR_MODE_TV", "strict")     # strict|lenient (see filters/aspect_ratio.py)
AR_MODE_MOVIE = os.environ.get("SP_AR_MODE_MOVIE", "lenient")  # strict|lenient
# What a FAILING filter does to the download:
#   soft (default) — keep the file, finish the job, attach a filter_warnings
#                    note to the job row + .json sidecar (still watchable)
#   hard           — discard the file, fail this link, try the next one
AR_ACTION = os.environ.get("SP_AR_ACTION", "soft")   # soft|hard
AR_FFPROBE_BIN = os.environ.get("SP_FFPROBE_BIN", "ffprobe")

# duration filter tuning (catches wrong-content: a movie served for a TV ep)
DUR_TOLERANCE = float(os.environ.get("SP_DUR_TOLERANCE", "0.6"))  # ±60% of expected runtime
# hard (default) — wrong content must NOT land in the library; discard + try
# next link. soft — keep the file, attach a filter_warnings note.
DUR_ACTION = os.environ.get("SP_DUR_ACTION", "hard")   # soft|hard

# Per-URL exclusions (2026-08-07): block individual bad links, not providers.
# A provider may return 5 links, 2 good / 3 junk — junk URLs get auto-blocked
# after BLOCK_URL_AFTER_FAILURES consecutive failures (yt-dlp error) or one
# hard-filter rejection (wrong content), while the provider stays usable.
BLOCK_URL_ENABLED = os.environ.get("SP_BLOCK_URL_ENABLED", "1") not in ("0", "false", "False")
BLOCK_URL_AFTER_FAILURES = int(os.environ.get("SP_BLOCK_URL_AFTER_FAILURES", "2"))
