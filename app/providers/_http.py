"""Shared HTTP + embed-page helpers.

Includes the Dean Edwards p.a.c.k.e.r unpacker used by uqload-style players
(the same decoder the CloudStream app's extractor API performs in Kotlin).
"""
from __future__ import annotations

import re

import httpx

from .. import config

_CLIENT = httpx.Client(
    timeout=config.REQUEST_TIMEOUT,
    follow_redirects=True,
    headers={"User-Agent": config.USER_AGENT},
)


def get(url: str, *, referer: str | None = None, headers: dict | None = None, params: dict | None = None) -> httpx.Response:
    h = dict(headers or {})
    if referer:
        h["Referer"] = referer
    return _CLIENT.get(url, headers=h, params=params)


def post(url: str, *, data=None, referer: str | None = None, headers: dict | None = None, params: dict | None = None) -> httpx.Response:
    h = dict(headers or {})
    if referer:
        h["Referer"] = referer
    return _CLIENT.post(url, data=data, headers=h, params=params)


# ---- Dean Edwards p.a.c.k.e.r unpacker --------------------------------------

_PACKER_RE = re.compile(
    r"eval\(function\(p,a,c,k,e,d\)\{[^}]*\}\('(.*?)',\s*(\d+),\s*(\d+),\s*'(.*?)'\.split\('\|'\)",
    re.S,
)


def unpack_packed_js(html: str) -> str | None:
    """Decode a Dean Edwards packed JS block. Returns the decoded JS or None."""
    m = _PACKER_RE.search(html)
    if not m:
        return None
    payload, base_s, count_s, words_s = m.group(1), m.group(2), m.group(3), m.group(4)
    try:
        base = int(base_s)
    except ValueError:
        base = 36
    words = words_s.split("|")

    def repl(match):
        tok = match.group(0)
        try:
            n = int(tok, base)
        except ValueError:
            return tok
        return words[n] if n < len(words) else tok

    return re.sub(r"\b[0-9a-zA-Z]+\b", repl, payload)


def extract_hls_from_packed(decoded_js: str | None) -> list[str]:
    """From a decoded uqload player script, pull the HLS source URLs (hls3 preferred)."""
    if not decoded_js:
        return []
    m = re.search(r"var links=\{(.*?)\};", decoded_js, re.S)
    if not m:
        return []
    urls = re.findall(r'"(https?://[^"]+)"', m.group(1))
    # prefer hls3 (master.txt) over hls2 (master.m3u8) — verified hls2 is 403
    hls3 = [u for u in urls if "hls3" in u]
    hls2 = [u for u in urls if "hls2" in u]
    return hls3 + hls2


def base_url(url: str) -> str:
    m = re.match(r"(https?://[^/]+)", url)
    return m.group(1) if m else url
