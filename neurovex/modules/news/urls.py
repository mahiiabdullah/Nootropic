"""URL canonicalization and title fingerprints, the two layers of dedupe."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "ref", "ref_src",
    "cmpid", "ncid", "sr_share", "guccounter", "_hsenc", "_hsmi", "mkt_tok", "yclid",
}  # fmt: skip


def canonicalize_url(url: str) -> str:
    """Normalize a URL so the same article from different links maps to one key.

    Lowercases scheme/host, drops `www.`, default ports, fragments, tracking
    params (`utm_*` and friends) and trailing slashes; sorts remaining params.
    """
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    if scheme == "http":
        scheme = "https"
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = parts.port
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"

    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    ]
    query.sort()
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


def title_fingerprint(title: str) -> str:
    """Hash of a title reduced to lowercase alphanumerics, for cross-source dedupe."""
    norm = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def is_http_url(url: str) -> bool:
    parts = urlsplit(url.strip())
    return parts.scheme in ("http", "https") and bool(parts.hostname)
