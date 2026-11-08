"""Why a download failed, in one word, with the action that follows from it.

The 2026-10 audit produced a hard lesson: "it did not download" is not a
diagnosis. On a campus network with full entitlement, every failure was a *bot*
wall, and no amount of retrying or proxy fiddling would have helped. The useful
output is therefore a classification:

    pdf          the bytes are the paper -- fetch should have stored it
    cloudflare   "Just a moment..." interstitial; solve in a real browser
    botwall      Incapsula/Imperva/Distil/Akamai bot wall; same action as cloudflare
    challenge    a small HTML page asking to verify you are human
    paywall      the publisher is asking for money or a login
    js_required  the server answers an HTML shell; a browser must run it
    dead         the link is gone (404/410)
    not_pdf      something else came back (login page, error page, landing page)

`action` turns each into the next concrete step, because the only thing worse
than a failure is a failure that invites another retry of the same tier.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

ACTION = {
    "pdf": "store it (fetch does this automatically)",
    "cloudflare": "open in your own browser and use `litfetch import`",
    "challenge": "open in your own browser and use `litfetch import`",
    "botwall": "open in your own browser and use `litfetch import`",
    "paywall": "no access from this network; use the library proxy or ILL",
    "js_required": "retry with --browser, or import by hand",
    "dead": "find another copy (OpenAlex / Semantic Scholar / author page)",
    "not_pdf": "look at the page: usually a landing page or a login redirect",
    "error": "transport-level failure; retry once, then treat as dead",
}

MARKERS = [
    ("cloudflare", re.compile(rb"just a moment|cf-browser-verification|"
                              rb"checking your browser|cf_chl_opt|__cf_chl", re.I)),
    ("botwall", re.compile(rb"_Incapsula_Resource|incapsula|imperva|distil_referrer|"
                           rb"akamai bot|are you a robot", re.I)),
    ("challenge", re.compile(rb"verify you are (a )?human|captcha|"
                             rb"security (check|verification)|are you a robot", re.I)),
    ("paywall", re.compile(rb"access (this|the) (content|article)|purchase (this|access)|"
                           rb"subscribe to (read|view)|sign in to (view|access)|"
                           rb"rent this article", re.I)),
    ("js_required", re.compile(rb"<script[^>]*src=|enable javascript", re.I)),
]


@dataclass
class Probe:
    url: str
    status: int = 0
    content_type: str = ""
    size: int = 0
    verdict: str = "error"
    detail: str = ""

    @property
    def action(self) -> str:
        return ACTION.get(self.verdict, ACTION["error"])

    def as_dict(self) -> dict:
        return {"url": self.url, "status": self.status, "type": self.content_type,
                "bytes": self.size, "verdict": self.verdict,
                "action": self.action, "detail": self.detail}


def classify(status: int, content_type: str, head: bytes, size: int) -> tuple:
    """Return (verdict, detail). `head` is the first few KB of the body."""
    ctype = (content_type or "").lower()
    if head[:4] == b"%PDF":
        return "pdf", "PDF magic number"
    if status in (404, 410):
        return "dead", f"HTTP {status}"
    if status in (401, 402, 407):
        return "paywall", f"HTTP {status}"
    for name, rx in MARKERS:
        if rx.search(head or b""):
            return name, f"HTTP {status}, {ctype or 'no content-type'}, {size} bytes"
    if "pdf" in ctype:
        return "pdf", "content-type says PDF but the magic number does not"
    if status >= 500:
        return "js_required", f"HTTP {status}"
    if "html" in ctype:
        return "not_pdf", f"HTML page ({size} bytes)"
    return "not_pdf", f"HTTP {status}, {ctype or 'unknown type'}, {size} bytes"


def probe(url: str, timeout: float = 20.0, read: int = 65536) -> Probe:
    """Fetch only the head of the response and classify it. Never raises."""
    p = Probe(url=url)
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as fh:
            p.status = getattr(fh, "status", 200)
            p.content_type = fh.headers.get("content-type", "")
            body = fh.read(read)
            p.size = len(body)
            p.verdict, p.detail = classify(p.status, p.content_type, body, p.size)
    except urllib.error.HTTPError as exc:
        p.status = exc.code
        try:
            body = exc.read(read)
        except Exception:
            body = b""
        p.content_type = exc.headers.get("content-type", "") if exc.headers else ""
        p.size = len(body)
        p.verdict, p.detail = classify(p.status, p.content_type, body, p.size)
    except Exception as exc:
        p.verdict, p.detail = "error", f"{type(exc).__name__}: {exc}"
    if p.verdict == "not_pdf" and "/tdm/" in url and p.status in (400, 401, 403):
        p.verdict, p.detail = "paywall", f"TDM API requires an institutional token (HTTP {p.status})"
    return p


def diagnose(resolved, timeout: float = 20.0) -> List[Probe]:
    """Probe every candidate of a resolved reference, in tier order."""
    out = []
    for tier, url in resolved.ordered():
        p = probe(url, timeout=timeout)
        p.detail = f"[{tier}] " + p.detail
        out.append(p)
        if p.verdict == "pdf":
            break
    return out


def summarise(probes: List[Probe]) -> str:
    if not probes:
        return "no candidate URL"
    if any(p.verdict == "pdf" for p in probes):
        return "retrievable"
    codes = [p.verdict for p in probes]
    if "cloudflare" in codes or "challenge" in codes or "botwall" in codes:
        return "bot wall -- a human browser is required"
    if "paywall" in codes:
        return "no entitlement from this network"
    if "js_required" in codes:
        return "needs a browser session (--browser)"
    return "dead or misdirected links"
