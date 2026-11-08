"""Reachability classification for a reference list.

Given a reference (one or more URLs, possibly a DOI), decide, *statically* (no
network), how a PDF for it can be obtained from the current environment:

    AUTO        -- this sandbox can fetch it with curl + verification
    OFFICE_SUB  -- subscription wall; needs an institution's credentials
    BROWSER     -- open access, but JS/anti-bot gated; needs a real browser
                   (the office machine, or any interactive browser)
    REPO        -- it is a code/dataset repository, not a PDF (git clone)
    UNKNOWN     -- could not be classified

This is the capability that used to live in an ad-hoc project script
(`crossscale_lit/fetcher_autofetch.py`). It is now part of litfetch so that
``litfetch classify refs.json`` produces the plan and ``--fetch`` downloads the
AUTO ones, with the same byte-level verification as every other tier.

Every rule here was forced by a real failure:

  * PMLR paper IDs carry a ``.html`` suffix on the landing page; naively
    stripping the path gives ``orujlu26a.html.pdf`` (404). The real file is at
    ``raw.githubusercontent.com/mlresearch/v{vol}/.../{id}/{id}.pdf``.
  * PMC (and NCBI) serve a *"Preparing to download …"* 1-page PDF stub to curl;
    it is a valid PDF so a magic-byte check would accept it. We reject any
    1-page file whose text contains that marker.
  * MDPI / Frontiers / PLOS are OA but return 403 to scripted curl from this
    host. They are not paywalled -- they just need a browser. Verdict BROWSER,
    not OFFICE_SUB.
  * A 403 from PNAS / OUP / Wiley / Taylor&Francis / ScienceDirect is a
    Cloudflare "Just a moment…" challenge, **not** a subscription refusal. On a
    campus network the credentials are irrelevant to a script: the request
    leaves through the sandbox's fixed proxy IP, and CF judges JS execution.
    So "we are on the campus net now" does not unlock scripted downloads.
  * A subscription DOI is NOT a dead end: OpenAlex / Unpaywall / Crossref often
    index a legitimate OA preprint (usually arXiv). In the 2026-11-02 campus
    run, 10 of 34 paywalled DOIs had an arXiv copy that curl could fetch, and
    all 10 were verified against the Crossref/OpenAlex title before storing.
    ``classify --oa-lookup`` does this automatically. The catch: an OA copy is a
    **preprint**, not the version of record -- record that, never silently
    treat it as the journal article.
"""
from __future__ import annotations

import json
import re
import subprocess
import urllib.parse
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

try:
    import pymupdf  # type: ignore
except Exception:  # pragma: no cover
    pymupdf = None


# Verdicts returned by classify()
AUTO = "AUTO"
OFFICE_SUB = "OFFICE_SUB"
BROWSER = "BROWSER"
REPO = "REPO"
ALT_SOURCE = "ALT_SOURCE"
OA_COPY = "OA_COPY"      # paywalled DOI, but a verified OA preprint exists
UNKNOWN = "UNKNOWN"

# Host substrings that are open access but bot-walled from scripted curl here.
_BROWSER_HOSTS = (
    "mdpi.com",
    "frontiersin.org",
    "plos.org",
    "europepmc.org",
)

# Host substrings that are subscription walls needing institution creds.
_SUB_HOSTS = (
    "doi.org",
    "onlinelibrary.wiley.com",
    "sciencedirect.com",
    "elsevier.com",
    "academic.oup.com",
    "tandfonline.com",
    "sagepub.com",
    "journals.aps.org",
    "ams.org",
    "epubs.siam.org",
    "ieee.org",
    "acm.org",
    "nowpublishers.com",
    "annualreviews.org",
    "cambridge.org",          # non-AOP Cambridge is paywalled
    "projecteuclid.org",
    "ssrn.com",
    "jstor.org",
    "springer.com",           # landing/DOI; direct /content/pdf is AUTO
    "nature.com",             # only the direct *.pdf slug is AUTO
    "science.org",
    "sciencemag.org",
    "pnas.org",               # only the direct /doi/pdf slug is AUTO
    "wiley.com",
    "wiley-vch.de",
)

# Direct-PDF host slugs that proved reachable from this sandbox (curl + verify).
_AUTO_DIRECT_PATTERNS = (
    r"nature\.com/.*\.pdf",
    r"link\.springer\.com/content/pdf/",
    r"sciencemag\.org/.*\.pdf",
    r"science\.sciencemag\.org/.*\.pdf",
    r"pnas\.org/doi/pdf/",
    r"\.science\.org/doi/pdf/",
)


@dataclass
class Cand:
    """One download attempt.

    kind == 'url'    -> fetch ``url`` directly (it is (or should be) a PDF)
    kind == 'scrape' -> fetch ``url`` as a landing page, extract its .pdf links
    """
    kind: str
    url: str


@dataclass
class Verdict:
    num: str
    title: str
    link: str
    verdict: str
    candidates: List[Cand] = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "num": self.num,
            "title": self.title,
            "link": self.link,
            "verdict": self.verdict,
            "note": self.note,
            "candidates": [{"kind": c.kind, "url": c.url} for c in self.candidates],
        }


# --------------------------------------------------------------------------
# per-URL classification
# --------------------------------------------------------------------------

def _host(u: str) -> str:
    try:
        h = urllib.parse.urlparse(u).netloc.lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def _arxiv_id(u: str) -> Optional[str]:
    m = re.search(r"arxiv\.org/(?:abs|pdf|html)/(\d+\.\d+(?:v\d+)?)", u)
    return m.group(1) if m else None


def _pmlr(u: str):
    m = re.search(r"/v(\d+)/([\w.\-]+?)(?:\.html)?$", u)
    if m:
        return m.group(1), m.group(2)
    return None


def _pmc_id(u: str) -> Optional[str]:
    m = re.search(r"PMC(\d+)", u)
    return m.group(1) if m else None


def _scrape_cands(u: str) -> List[Cand]:
    """Landing-page candidates: scrape for .pdf + a few common paper.pdf guesses."""
    cands = [Cand("scrape", u)]
    base = u.rstrip("/")
    for guess in ("/paper.pdf", "/assets/paper.pdf", "/main.pdf", "/files/paper.pdf"):
        cands.append(Cand("url", base + guess))
    return cands


def url_verdict(u: str) -> Verdict:
    """Classify a single URL into (verdict, candidates, note)."""
    h = _host(u)

    # Code / dataset repositories -- not a PDF.
    if h == "github.com" or h.endswith(".github.com"):
        return Verdict("", "", u, REPO, [], "code/dataset repo -> git clone (not a PDF)")

    # arXiv -- always AUTO.
    aid = _arxiv_id(u)
    if aid:
        return Verdict("", "", u, AUTO,
                       [Cand("url", f"https://arxiv.org/pdf/{aid}"),
                        Cand("url", f"https://export.arxiv.org/pdf/{aid}")],
                       "arXiv open access")

    # PMLR -- AUTO via the mlresearch raw-github mirror (landing .html name bug fixed).
    pm = _pmlr(u)
    if pm:
        v, name = pm
        return Verdict("", "", u, AUTO,
                       [Cand("url", f"https://proceedings.mlr.press/v{v}/{name}.pdf"),
                        Cand("url", f"https://raw.githubusercontent.com/mlresearch/v{v}/main/assets/{name}/{name}.pdf"),
                        Cand("url", f"https://raw.githubusercontent.com/mlresearch/v{v}/master/assets/{name}/{name}.pdf")],
                       "PMLR open access (mlresearch mirror)")

    # CVF / ICLR -- AUTO, scrape the landing for the PDF link.
    if h == "openaccess.thecvf.com" or h == "proceedings.iclr.cc":
        return Verdict("", "", u, AUTO, _scrape_cands(u),
                       "open-access conference proceedings (scrape landing)")

    # PubMed Central -- OA but JS-gated; a real browser is required.
    if h == "pmc.ncbi.nlm.nih.gov":
        pmc = _pmc_id(u)
        cands = [Cand("url", f"https://pmc.ncbi.nlm.nih.gov/articles/PMC{pmc}/pdf/")] if pmc else []
        return Verdict("", "", u, BROWSER, cands,
                       "OA but PDF is JS-gated; use a browser (office) then `litfetch import`")

    # PubMed abstract page -- resolve to PMC, but still needs a browser.
    if h == "pubmed.ncbi.nlm.nih.gov":
        return Verdict("", "", u, BROWSER, [Cand("scrape", u)],
                       "abstract page; resolve PMCID, then OA-JS-gated -> browser")

    # Direct publisher-PDF slugs that are reachable from this sandbox.
    for pat in _AUTO_DIRECT_PATTERNS:
        if re.search(pat, u):
            return Verdict("", "", u, AUTO, [Cand("url", u)],
                           "direct publisher PDF reachable from sandbox")

    # Cambridge's open-access content service (AOP) -- reachable from sandbox,
    # unlike the paywalled Cambridge DOI/landing pages.
    if "cambridge.org/core/services/aop" in u:
        return Verdict("", "", u, AUTO, [Cand("url", u)],
                       "Cambridge open-access content service")

    # Cambridge *journal landing pages* are not necessarily paywalled: Acta
    # Numerica and other OA articles have a downloadable AOP copy behind the
    # article page. Filing these as OFFICE_SUB sends the reader to the office
    # for a PDF that a scrape could have found. Attempt the landing page.
    if "cambridge.org/core/journals/" in u:
        return Verdict("", "", u, AUTO, [Cand("scrape", u)],
                       "Cambridge journal landing; scrape for the AOP/OA PDF link")

    # github.io / author / project pages -- AUTO, scrape + guesses.
    if h.endswith("github.io") or "author" in h or "research" in h or \
       h in ("maxkasy.github.io", "tusz-agdr.github.io", "droid-dataset.github.io",
             "kurbanintelligencelab.github.io", "scientific-discovery.github.io"):
        return Verdict("", "", u, AUTO, _scrape_cands(u),
                       "author/project page (scrape + paper.pdf guesses)")

    # Institutional / working-paper OA pages -- AUTO, best-effort scrape.
    if any(h.endswith(x) for x in (
            "microsoft.com", "clevelandfed.org", "ifo.de", "cemmap.ac.uk",
            "researchconnect.suny.edu", "portal.research.lu.se", "escholarship.org",
            "nber.org", "csail.mit.edu", "mit.edu", "stanford.edu", "berkeley.edu",
            "jmlr.org", "proceedings.mlr.press")):
        return Verdict("", "", u, AUTO, _scrape_cands(u),
                       "institutional / working-paper page (scrape)")

    # Bot-walled OA hosts.
    if any(x in h for x in _BROWSER_HOSTS):
        return Verdict("", "", u, BROWSER, [Cand("url", u)],
                       "OA but anti-bot 403 to curl; use a browser")

    # Subscription walls.
    if any(x in h for x in _SUB_HOSTS):
        return Verdict("", "", u, OFFICE_SUB, [],
                       "subscription wall; needs institution credentials")

    # Fallback: try scraping whatever it is.
    return Verdict("", "", u, AUTO, _scrape_cands(u),
                   "unrecognised host; attempt scrape + guesses")


# --------------------------------------------------------------------------
# campus-network findings: Cloudflare challenges and OA preprint discovery
# --------------------------------------------------------------------------

_CF_MARKERS = ("just a moment", "cf-challenge", "checking your browser",
               "enable javascript and cookies to continue", "attention required")


def is_cloudflare_challenge(body: bytes) -> bool:
    """A 403 with a CF challenge page is bot detection, NOT a paywall verdict."""
    if not body or body[:4] == b"%PDF":
        return False
    head = body[:20000].decode("utf-8", "ignore").lower()
    return any(m in head for m in _CF_MARKERS)


def _api_json(url: str, timeout: float = 15):
    try:
        r = subprocess.run(["curl", "-sSL", "--max-time", str(timeout), url],
                           capture_output=True, timeout=timeout + 8)
        return json.loads(r.stdout.decode("utf-8", "ignore"))
    except Exception:
        return None


def discover_oa(doi: str, mailto: str = "", timeout: float = 15) -> dict:
    """For a subscription DOI, look for a legitimate OA preprint copy.

    Returns ``{"oa_pdf", "arxiv_pdf", "declared_title", "source"}``. The
    ``declared_title`` (from Crossref / OpenAlex) is what the downloaded PDF's
    title block must be checked against -- an arXiv id from an OA index can
    point at a different paper, so identity is never assumed from the id.

    Network calls live here (io layer); the caller decides whether to enable
    them, so plain ``classify`` stays static and offline-reproducible.
    """
    out = {"oa_pdf": None, "arxiv_pdf": None, "declared_title": "", "source": ""}
    if not doi:
        return out
    enc = urllib.parse.quote(doi, safe="")
    email = mailto or "litfetch@localhost"

    cr = _api_json(f"https://api.crossref.org/works/{enc}", timeout)
    if cr and isinstance(cr, dict):
        t = (cr.get("message") or {}).get("title") or []
        if t:
            out["declared_title"] = t[0]
            out["source"] = "crossref"

    up = _api_json(f"https://api.unpaywall.org/v2/{enc}?email={email}", timeout)
    oa_urls: List[str] = []
    if up and isinstance(up, dict):
        for loc in ([up.get("best_oa_location")] + (up.get("oa_locations") or [])):
            if loc and (loc.get("url_for_pdf") or loc.get("url")):
                u = loc.get("url_for_pdf") or loc.get("url")
                if u and u not in oa_urls:
                    oa_urls.append(u)

    ox = _api_json(
        f"https://api.openalex.org/works/https://doi.org/{enc}?mailto={email}", timeout)
    if ox and isinstance(ox, dict):
        if not out["declared_title"] and ox.get("title"):
            out["declared_title"] = ox["title"]
            out["source"] = "openalex"
        for loc in ([ox.get("best_oa_location")] + (ox.get("locations") or [])):
            if loc and loc.get("pdf_url"):
                u = loc["pdf_url"]
                if u and u not in oa_urls:
                    oa_urls.append(u)

    for u in oa_urls:
        m = re.search(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5}(?:v\d+)?)", u, re.I)
        if m:
            out["arxiv_pdf"] = f"https://arxiv.org/pdf/{m.group(1)}"
            break
        if "arxiv.org/pdf/" in u:
            out["arxiv_pdf"] = u
            break
    if not out["arxiv_pdf"]:
        for u in oa_urls:
            if u.lower().endswith(".pdf") and "doi.org/" not in u:
                out["oa_pdf"] = u
                break
    if not out["source"]:
        out["source"] = "unpaywall/openalex"
    return out


# --------------------------------------------------------------------------
# aggregate classification for one reference
# --------------------------------------------------------------------------

def classify(num: str, title: str, urls: List[str], doi: str = "",
             oa_lookup: bool = False, mailto: str = "") -> Verdict:
    """Classify a whole reference.

    Preference order across its URLs: AUTO > BROWSER > REPO > OFFICE_SUB > UNKNOWN.
    We surface AUTO whenever *any* URL is auto-fetchable, because that is the path
    the ``--fetch`` step will take.
    """
    sources = list(urls)
    if doi and f"doi.org/{doi}" not in " ".join(sources):
        sources.append(f"https://doi.org/{doi}")

    per = [url_verdict(u) for u in sources]
    link = sources[0] if sources else ""

    # preference
    for want in (AUTO, BROWSER, REPO, OFFICE_SUB):
        hits = [p for p in per if p.verdict == want]
        if hits:
            cands: List[Cand] = []
            for p in hits:
                cands.extend(p.candidates)
            note = hits[0].note
            if want == AUTO and len(hits) < len(per):
                note += " (other URLs need a browser/subscription)"
            if want == OFFICE_SUB and oa_lookup and doi:
                found = discover_oa(doi, mailto=mailto)
                pdf = found.get("arxiv_pdf") or found.get("oa_pdf")
                if pdf:
                    return Verdict(
                        num, title, link, OA_COPY, [Cand("url", pdf)],
                        "paywalled DOI but an OA preprint copy exists ("
                        + found.get("source", "") + "); VERIFY the title block "
                        "against the DOI title, and record version=preprint. "
                        "declared_title=" + (found.get("declared_title") or "?"))
                note += " (no OA preprint found in OpenAlex/Unpaywall/Crossref)"
            return Verdict(num, title, link, want, cands, note)

    return Verdict(num, title, link, UNKNOWN, [], "could not classify")


# --------------------------------------------------------------------------
# fetching the AUTO ones (network)
# --------------------------------------------------------------------------

def _curl_get(url: str, timeout: float = 25, retries: int = 2):
    cmd = ["curl", "-sSL", "--max-time", str(timeout), "-w", "\n%{http_code}", url]
    last = ("000", b"")
    for _ in range(retries + 1):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout + 12)
            out = r.stdout
            if b"\n" in out:
                code = out.rsplit(b"\n", 1)[1].decode(errors="ignore")
                body = out.rsplit(b"\n", 1)[0]
            else:
                code, body = "000", out
            if code == "200":
                return code, body
            last = (code, body)
        except Exception:
            pass
    return last


def _valid_pdf_bytes(b: bytes):
    if pymupdf is None:
        return (b[:4] == b"%PDF"), 0
    try:
        d = pymupdf.open(stream=b, filetype="pdf")
        if d.page_count == 0:
            return False, 0
        if d.page_count == 1:
            t = d[0].get_text().lower()
            if "preparing to download" in t or "vulnerability disclosure" in t:
                return False, 0  # PMC JS-gated stub, not a real article
        return True, d.page_count
    except Exception:
        return False, 0


def _pdf_links_from_html(html: str, base: str) -> List[str]:
    out = []
    for m in re.finditer(r'(?:href|src|data-src)=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']', html, re.I):
        l = m.group(1)
        if l.startswith("//"):
            l = "https:" + l
        elif l.startswith("/"):
            p = urllib.parse.urlparse(base)
            l = f"{p.scheme}://{p.netloc}{l}"
        elif not l.startswith("http"):
            l = urllib.parse.urljoin(base, l)
        out.append(l)
    return out


def auto_land(cands: List[Cand], timeout: float = 25) -> Optional[tuple]:
    """Try each candidate; return (path_bytes, pages, used_url) or None.

    Does NOT write to disk -- the caller stores via the library. Returns the raw
    bytes so the caller can stream them straight into the library without a temp
    file round-trip.
    """
    for c in cands:
        if c.kind == "url":
            code, body = _curl_get(c.url, timeout)
            if code == "200" and body[:4] == b"%PDF":
                ok, n = _valid_pdf_bytes(body)
                if ok:
                    return body, n, c.url
        else:  # scrape landing page
            code, html = _curl_get(c.url, timeout)
            if code != "200" or not html:
                continue
            for link in _pdf_links_from_html(html.decode("utf-8", "ignore"), c.url):
                if "/pdf/" in link or link.endswith(".pdf"):
                    code2, body2 = _curl_get(link, timeout)
                    if code2 == "200" and body2[:4] == b"%PDF":
                        ok, n = _valid_pdf_bytes(body2)
                        if ok:
                            return body2, n, link
    return None
