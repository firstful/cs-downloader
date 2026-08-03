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
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("SP_MAX_CONCURRENT", "2"))
MAX_RETRIES = int(os.environ.get("SP_MAX_RETRIES", "2"))

# Rate limiting: max bytes per rolling window (default 10GiB / 10 min)
RATE_LIMIT_BYTES = int(os.environ.get("SP_RATE_LIMIT_BYTES", str(10 * 1024 ** 3)))
RATE_WINDOW_SECONDS = int(os.environ.get("SP_RATE_WINDOW_SECONDS", "600"))

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
