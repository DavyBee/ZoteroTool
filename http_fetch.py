"""
http_fetch.py — Every web request for page content and PDFs goes through fetch().

Many publisher sites reject plain scripted requests based on how the
connection looks. curl_cffi makes the request look like a normal browser, which
gets past some of these (ScienceDirect, Duke University Press, ...). Sites that
require running JavaScript first (SSRN, JAMA, BMJ) still refuse; those items go
to Review as "Page could not be accessed" unless another step finds them.

If curl_cffi isn't available, plain urllib is used instead.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Optional

import config

try:
    from curl_cffi import requests as _cffi
except Exception:           # ImportError, or a broken/incompatible native wheel
    _cffi = None

from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# curl_cffi impersonation target. "chrome" tracks the newest fingerprint the
# installed curl_cffi knows about; bumping the package is what keeps it current.
IMPERSONATE = "chrome"

_ACCEPT = ("text/html,application/xhtml+xml,application/xml;q=0.9,"
           "application/pdf,*/*;q=0.8")
_HEADERS = {"Accept": _ACCEPT, "Accept-Language": "en-US,en;q=0.9"}



# A scan reads the same page up to three times (PDF search, citation tags, the
# AI step). A short-lived cache makes those one request, and guarantees every
# step sees the same copy even when a site alternates between serving and
# blocking. Bodies over CACHE_MAX_BODY (large PDFs) are not kept.
CACHE_TTL = 15 * 60
CACHE_MAX_BODY = 5 * 1024 * 1024
CACHE_MAX_TOTAL = 100 * 1024 * 1024
_cache: "OrderedDict[str, tuple[float, Optional[bytes], str, int, int]]" = OrderedDict()
_cache_lock = threading.Lock()


def _cache_get(url: str, max_bytes: int):
    with _cache_lock:
        entry = _cache.get(url)
        if entry is None:
            return None
        at, body, ct, status, cap = entry
        if time.monotonic() - at > CACHE_TTL:
            del _cache[url]
            return None
        # A body cut off at a smaller cap can't answer a request for more.
        if body is not None and len(body) >= cap and cap < max_bytes:
            return None
        _cache.move_to_end(url)
        return (body[:max_bytes] if body is not None else None), ct, status


def _cache_put(url: str, result, max_bytes: int) -> None:
    body, ct, status = result
    if body is not None and len(body) > CACHE_MAX_BODY:
        return
    with _cache_lock:
        _cache[url] = (time.monotonic(), body, ct, status, max_bytes)
        _cache.move_to_end(url)
        total = sum(len(e[1] or b"") for e in _cache.values())
        while total > CACHE_MAX_TOTAL and len(_cache) > 1:
            _, old = _cache.popitem(last=False)
            total -= len(old[1] or b"")


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def fetch(url: str, *, max_bytes: int, timeout: Optional[int] = None,
          retries: int = 1) -> tuple[Optional[bytes], str, int]:
    hit = _cache_get(url, max_bytes)
    if hit is not None:
        return hit
    result = _fetch(url, max_bytes=max_bytes, timeout=timeout, retries=retries)
    _cache_put(url, result, max_bytes)
    return result


def _fetch(url: str, *, max_bytes: int, timeout: Optional[int] = None,
           retries: int = 1) -> tuple[Optional[bytes], str, int]:
    """GET `url` like a browser. Returns (body, content_type, status):
      - 2xx        → (bytes, content_type, status_code)
      - non-2xx    → (None, "", status_code)   e.g. 404, 403, 503
      - network    → (None, "", 0)             DNS/connection/timeout, no HTTP
    Reads at most `max_bytes` (a runaway server can't fill memory).

    Only 404 and 410 mean the page is gone; 403, 429 and 5xx may just be a
    block. Retries once after a second, except on 404/410.
    """
    timeout = timeout or config.HTTP_TIMEOUT
    status = 0
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(1.0)
        body, ct, status = (_get_cffi(url, max_bytes, timeout) if _cffi
                            else _get_urllib(url, max_bytes, timeout))
        if body is not None:
            return body, ct, status
        if status in (404, 410):          # gone; don't retry
            break
    return None, "", status


def _get_cffi(url, max_bytes, timeout):
    """Browser-impersonating fetch via curl_cffi. Streams so the byte cap is
    honoured even when the server ignores length headers. Third return value is
    the HTTP status (0 if no response was received)."""
    try:
        r = _cffi.get(url, impersonate=IMPERSONATE, headers=_HEADERS,
                      timeout=timeout, stream=True)
    except Exception:
        return None, "", 0
    try:
        if not (200 <= r.status_code < 300):
            return None, "", r.status_code   # 403 challenge, 404 gone, etc.
        ct = (r.headers.get("Content-Type") or "").lower()
        buf = bytearray()
        for chunk in r.iter_content():
            buf += chunk
            if len(buf) >= max_bytes:
                break
        return bytes(buf[:max_bytes]), ct, r.status_code
    except Exception:
        return None, "", 0
    finally:
        try:
            r.close()
        except Exception:
            pass


def _get_urllib(url, max_bytes, timeout):
    """Used when curl_cffi is unavailable: urllib with a browser User-Agent.
    The status is 0 when no HTTP response arrived."""
    req = Request(url, headers={"User-Agent": config.BROWSER_USER_AGENT, **_HEADERS})
    try:
        with urlopen(req, timeout=timeout) as resp:
            ct = (resp.headers.get("Content-Type") or "").lower()
            return resp.read(max_bytes), ct, getattr(resp, "status", 200)
    except HTTPError as e:
        return None, "", e.code             # the server answered with an error
    except (URLError, TimeoutError):
        return None, "", 0                  # no HTTP response (DNS/conn/timeout)
    except Exception:
        return None, "", 0
