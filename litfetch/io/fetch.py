"""The download loop: try the tiers, verify every byte, record what it is.

Policy encoded here (each clause paid for once):
  * write to a temp file, verify, and only then promote it into the library;
  * a failed attempt deletes its temp file and moves on -- never keep "maybe" files;
  * **a bot wall stops the remaining candidates on that host** (borrowed from
    scihub-cli's fail-fast). The 2026-10-22 batch hung for seven silent minutes
    because five candidate URLs on the same two hosts each waited out a timeout
    on a page that would never serve a PDF;
  * **the transport is retried once, the URL is not guessed** (borrowed from
    fulltext-article-downloader's `[tls]` extra). Measured here: a Chrome TLS
    fingerprint defeats plain UA/TLS filters but does **not** defeat Cloudflare's
    JavaScript challenge -- so it is tried only where the diagnosis says a
    fingerprint is the plausible blocker;
  * every stored file gets a provenance line saying *which version of the paper*
    it is. Passing four conditions proves identity, not version;
  * the report distinguishes `stored` from `duplicate`, because "4/4 stored" once
    counted three files that were already in the library.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlsplit

from ..core import provenance
from . import browser, diagnose, sources
from ..core.store import Library
from ..core.verify import Verdict, verify_pdf

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}
MAX_BYTES = 120 * 1024 * 1024

# Verdicts that mean "this host will not serve us a PDF today". Retrying another
# path on the same host is waste, and it is what made the batch look hung.
BLOCKING = {"cloudflare", "botwall", "challenge", "paywall"}

# Verdicts where a different TLS/HTTP client fingerprint is worth one retry.
FINGERPRINT_FIXABLE = {"not_pdf", "js_required"}

MULTI_PART_SUFFIXES = {"co.uk", "ac.uk", "org.uk", "com.au", "co.jp", "com.cn",
                       "ac.cn", "edu.cn", "co.in", "com.br", "co.nz"}


def worth_tls_retry(code: str, status: int, size: int, content_type: str) -> bool:
    """Can a different client fingerprint plausibly change this outcome?

    Measured on the 2026-10-22 batch: two of seven references spent their whole
    budget retrying a *784 KB HTML page* under a different TLS fingerprint, and
    one retried an HTTP 400. Neither can be fixed by looking more like a browser:

      * a 400 means the request itself is wrong for that endpoint;
      * a full-size HTML page is the site's real answer, not a challenge shell --
        the same HTML arrives at any fingerprint.

    A retry is only worth it for a small response, which is what a bot filter or
    a JS gate returns.
    """
    if code not in FINGERPRINT_FIXABLE:
        return False
    if status in (400, 401, 402, 404, 410):
        return False
    if size > 32768:          # a challenge shell is 1-10 KB; 46 KB is a page
        return False
    return True


def domain(url: str) -> str:
    """Registrable domain, coarsely: `academic.oup.com` -> `oup.com`.

    Not a public-suffix implementation, and it does not need to be. It only
    groups candidate URLs that will hit the same bot wall.
    """
    host = (urlsplit(url).hostname or "").lower()
    if not host or host.replace(".", "").isdigit():
        return host
    parts = host.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in MULTI_PART_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def tls_available() -> bool:
    try:
        import curl_cffi  # noqa: F401
        return True
    except Exception:
        return False


@dataclass
class Response:
    """What one HTTP attempt produced, including where it ended up.

    `url` is the final URL after redirects, and it is load-bearing: a candidate
    `https://doi.org/10.1214/...` redirects to projecteuclid.org, so a bot wall
    met there must block the *publisher's* domain, not doi.org. Without the final
    URL the same wall is met again one candidate later.
    """

    ok: bool = False
    status: int = 0
    size: int = 0
    note: str = ""
    content_type: str = ""
    head: bytes = b""
    url: str = ""


@dataclass
class Outcome:
    ref: sources.Reference
    status: str                     # stored | duplicate | rejected | blocked | unavailable | skipped
    level: str = ""                 # L0 | L1 | L2 | L3 | manual
    title: str = ""
    path: str = ""
    verdict: Optional[Verdict] = None
    tried: List[str] = field(default_factory=list)
    note: str = ""
    blocked: str = ""               # the diagnose verdict that stopped us
    version: str = ""               # preprint | accepted | vor | unknown
    transport: str = ""

    def line(self) -> str:
        bits = [f"{self.ref.num or '-':>4}", f"{self.status:<11}",
                f"{self.level or '-':<6}", f"{self.title[:52]:<52}"]
        if self.version:
            bits.append(f"{self.version:<8}")
        if self.verdict and self.verdict.reasons:
            bits.append("; ".join(self.verdict.reasons))
        if self.note:
            bits.append(self.note)
        return "  ".join(bits)


def download(url: str, dest: Path, timeout: float = 60.0,
             transport: str = "plain") -> Response:
    """One attempt. `transport="tls"` replays a Chrome TLS fingerprint via
    curl_cffi when it is installed -- same URL, different client, which is what
    the diagnosis distinguishes from "different source"."""
    if transport == "tls":
        try:
            from curl_cffi import requests as curl_requests
        except Exception:
            return Response(note="curl_cffi not installed", url=url)
        try:
            r = curl_requests.get(url, impersonate="chrome", timeout=timeout,
                                  allow_redirects=True,
                                  headers={"Accept-Language": "en-US,en;q=0.9"})
            ctype = r.headers.get("content-type", "")
            final = str(getattr(r, "url", "") or url)
            body = r.content
            if body[:4] != b"%PDF":
                return Response(False, r.status_code, len(body), f"HTTP {r.status_code}",
                                ctype, body[:4096], final)
            dest.write_bytes(body)
            return Response(True, r.status_code, len(body), "", ctype, body[:4096], final)
        except Exception as exc:
            return Response(False, 0, 0, f"{type(exc).__name__}", "", b"", url)

    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as fh:
            status = getattr(fh, "status", 200)
            ctype = fh.headers.get("content-type", "")
            final = fh.geturl() or url
            total = 0
            head = b""
            looks_html = "html" in ctype.lower()
            step = 8192 if looks_html else (1 << 16)
            with open(dest, "wb") as out:
                while True:
                    block = fh.read(step)
                    if not block:
                        break
                    if len(head) < 4096:
                        head += block[:4096 - len(head)]
                    total += len(block)
                    # A 784 KB landing page will be rejected whatever follows it;
                    # reading to the end only burns the budget. 64 KB is enough
                    # for every marker the classifier looks for.
                    if looks_html and total > 65536:
                        return Response(False, status, total, "HTML body (truncated)",
                                        ctype, head, final)
                    if total > MAX_BYTES:
                        return Response(False, status, total, "exceeds size cap",
                                        ctype, head, final)
                    out.write(block)
        return Response(True, status, total, "", ctype, head, final)
    except urllib.error.HTTPError as exc:
        try:
            head = exc.read(4096)
        except Exception:
            head = b""
        ctype = (exc.headers or {}).get("content-type", "")
        final = getattr(exc, "url", url) or url
        return Response(False, exc.code, 0, f"HTTP {exc.code}", ctype, head, final)
    except Exception as exc:  # timeouts, DNS, TLS
        return Response(False, 0, 0, type(exc).__name__, "", b"", url)


def verified_refs(log_path: str) -> set:
    """Reference numbers already stored according to an attempt log.

    `--resume` reads this, so a batch that died at reference 31 does not redo 30
    downloads (and does not hammer the same hosts again).
    """
    done = set()
    path = Path(log_path) if log_path else None
    if not path or not path.exists():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("result") == "verified" and ev.get("ref"):
            done.add(str(ev["ref"]))
    return done


def fetch_ref(
    ref: sources.Reference,
    library: Library,
    layer: Optional[str] = None,
    use_browser: bool = False,
    threshold: float = 0.8,
    dry_run: bool = False,
    require_text: bool = False,
    tiers_allowed: Optional[List[str]] = None,
    timeout: float = 60.0,
    max_seconds: Optional[float] = None,
    verbose: bool = False,
    log_path: Optional[str] = None,
    allow_tls: bool = True,
    version_required: Optional[str] = None,
    skip_existing: bool = False,
    min_bytes: int = 5000,
) -> Outcome:
    resolved = sources.resolve(ref)
    title = resolved.canonical_title or ref.title
    tiers = resolved.ordered()
    if tiers_allowed:
        wanted = {t.strip().upper() for t in tiers_allowed if t.strip()}
        tiers = [(t, u) for t, u in tiers if t in wanted]

    if dry_run:
        return Outcome(ref=ref, status="planned", title=title,
                       note=" -> ".join(f"{t}:{u[:60]}" for t, u in tiers))

    if skip_existing:
        from ..core.store import make_name
        guess = library.root / (layer or "") / make_name(
            ref.surname() or "anon", resolved.year or ref.year or "nd", title)
        if guess.exists():
            return Outcome(ref=ref, status="skipped", level="library", title=title,
                           path=str(guess), note="already in the library under this name")

    if not tiers and not use_browser:
        return Outcome(ref=ref, status="unavailable", level="manual", title=title,
                       note="no candidate URL found (and the browser tier was not enabled)")

    def record(event: dict) -> None:
        """Append one line per attempt. A killed run must still leave evidence:
        the 2026-10-22 batch died after seven silent minutes and left nothing."""
        if not log_path:
            return
        try:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            pass

    started = time.time()
    tmpdir = Path(tempfile.mkdtemp(prefix="litfetch-"))
    outcome = Outcome(ref=ref, status="unavailable", level="manual", title=title)
    blocked_domains: dict = {}
    tls_ok = allow_tls and tls_available()

    try:
        for level, url in tiers:
            if max_seconds and time.time() - started > max_seconds:
                outcome.note = f"budget of {max_seconds:.0f}s exhausted"
                record({"ref": ref.num, "tier": level, "url": url,
                        "result": "skipped", "note": outcome.note})
                break

            dom = domain(url)
            if dom in blocked_domains:
                record({"ref": ref.num, "tier": level, "url": url,
                        "result": "skipped",
                        "note": f"host blocked earlier in this reference ({blocked_domains[dom]})"})
                if verbose:
                    print(f"    {ref.num:>4} {level}  skip {dom} ({blocked_domains[dom]})",
                          file=sys.stderr, flush=True)
                continue

            outcome.tried.append(url)
            tmp = tmpdir / "candidate.pdf"
            if verbose:
                print(f"    {ref.num:>4} {level}  {url[:96]}", file=sys.stderr, flush=True)

            resp = download(url, tmp, timeout=timeout)
            transport = "plain"
            code, detail = diagnose.classify(resp.status, resp.content_type,
                                             resp.head, resp.size)

            # One retry with a browser TLS fingerprint, but only where a
            # fingerprint is a plausible explanation for the failure. Measured
            # 2026-10-22: it defeats a plain UA/TLS filter and does NOT defeat
            # Cloudflare's JavaScript challenge, so a cloudflare verdict is not
            # retried at all.
            if (not resp.ok or code != "pdf") and tls_ok and worth_tls_retry(
                    code, resp.status, resp.size, resp.content_type):
                if verbose:
                    print(f"    {ref.num:>4} {level}  retry with TLS impersonation",
                          file=sys.stderr, flush=True)
                retry = download(url, tmp, timeout=timeout, transport="tls")
                code2, detail2 = diagnose.classify(retry.status, retry.content_type,
                                                   retry.head, retry.size)
                record({"ref": ref.num, "tier": level, "url": url,
                        "result": "tls-retry", "transport": "tls",
                        "verdict": code2, "note": detail2})
                if code2 == "pdf":
                    resp, code, detail, transport = retry, code2, detail2, "tls"
                elif code == "not_pdf" and code2 in BLOCKING:
                    code, detail = code2, detail2    # the retry learned something

            ok, status, size, note = resp.ok, resp.status, resp.size, resp.note

            if not ok and code == "pdf":
                code, detail = "not_pdf", note or "download failed"

            if code != "pdf":
                outcome.note = f"{code}: {detail}"
                if code in BLOCKING:
                    blocked_domains[dom] = code
                    # The redirect target matters more than the candidate: doi.org
                    # forwards into the publisher, and the next candidate would
                    # otherwise walk straight into the same wall.
                    final_dom = domain(resp.url) if resp.url else ""
                    if final_dom and final_dom != dom:
                        blocked_domains[final_dom] = code
                    outcome.blocked = code
                record({"ref": ref.num, "tier": level, "url": url,
                        "result": "blocked" if code in BLOCKING else "download-failed",
                        "transport": transport, "verdict": code, "note": detail})
                try:
                    tmp.unlink()
                except OSError:
                    pass
                continue

            verdict = verify_pdf(
                tmp,
                expected_title=title or None,
                expected_year=resolved.year or ref.year or None,
                threshold=threshold,
                require_text=require_text,
                min_bytes=min_bytes,
            )
            verdict.path = str(tmp)
            outcome.verdict = verdict
            outcome.level = level
            outcome.transport = transport
            record({"ref": ref.num, "tier": level, "url": url,
                    "result": "verified" if verdict.ok else "rejected",
                    "transport": transport,
                    "coverage": round(verdict.coverage, 3),
                    "note": "; ".join(verdict.reasons)})
            if not verdict.ok:
                outcome.note = "; ".join(verdict.reasons)
                try:
                    tmp.unlink()
                except OSError:
                    pass
                continue

            prov = provenance.inspect_pdf(tmp, url=url, tier=level, transport=transport,
                                          title_declared=title, coverage=verdict.coverage)
            outcome.version = prov.version
            if version_required and prov.version != version_required:
                outcome.status = "rejected"
                outcome.note = (f"version is {prov.version} ({prov.version_basis}); "
                                f"required {version_required}")
                record({"ref": ref.num, "tier": level, "url": url,
                        "result": "version-mismatch", "version": prov.version,
                        "note": prov.version_basis})
                try:
                    tmp.unlink()
                except OSError:
                    pass
                continue

            stored, created = library.add_file_ex(
                tmp, author=ref.surname() or "anon",
                year=resolved.year or ref.year or "nd",
                title=title, layer=layer, move=True,
            )
            prov.file = str(stored)
            if created:
                provenance.append(library.root, [prov])
            outcome.status = "stored" if created else "duplicate"
            outcome.path = str(stored)
            if not created:
                outcome.note = "identical bytes already in the library"
            return outcome

        if use_browser:
            path, note = browser.grab(resolved.doi or ref.title, tmpdir, timeout=timeout)
            if path:
                verdict = verify_pdf(path, expected_title=title or None,
                                     expected_year=resolved.year or None,
                                     threshold=threshold, min_bytes=min_bytes)
                outcome.verdict, outcome.level = verdict, "L3"
                if verdict.ok:
                    prov = provenance.inspect_pdf(path, url=resolved.doi or "",
                                                  tier="L3", transport="chrome-cdp",
                                                  title_declared=title,
                                                  coverage=verdict.coverage)
                    stored, created = library.add_file_ex(
                        path, author=ref.surname() or "anon",
                        year=resolved.year or ref.year or "nd",
                        title=title, layer=layer)
                    prov.file = str(stored)
                    if created:
                        provenance.append(library.root, [prov])
                    outcome.status = "stored" if created else "duplicate"
                    outcome.path, outcome.version = str(stored), prov.version
                    outcome.transport = "chrome-cdp"
                    return outcome
                outcome.note = "; ".join(verdict.reasons)
            else:
                outcome.note = note or "browser tier unavailable"

        # One terminal state per reference, so the summary adds up.
        if outcome.blocked:
            outcome.status = "blocked"
            outcome.level = level if tiers else "manual"
        elif outcome.verdict is not None and not outcome.verdict.ok:
            outcome.status = "rejected"
        return outcome
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def fetch_all(refs, library: Library, **kwargs) -> List[Outcome]:
    results = []
    done = verified_refs(kwargs.get("log_path")) if kwargs.get("resume") else set()
    kwargs.pop("resume", None)
    for ref in refs:
        if str(ref.num) in done:
            results.append(Outcome(ref=ref, status="skipped", level="log",
                                   title=ref.title,
                                   note="already verified in this log (--resume)"))
            continue
        results.append(fetch_ref(ref, library, **kwargs))
    return results
