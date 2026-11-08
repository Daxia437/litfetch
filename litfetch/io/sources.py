"""Where PDFs come from, and in which order to try.

The ordering is the five-tier ladder that came out of the 2026-10 project audit:

    L0  arXiv / repository direct     (resolve the ID from the title, never trust an upstream ID)
    L1  Unpaywall / Crossref links    (the publisher's own declared open location)
    L2  publisher direct + browser headers
    L3  real Chrome session           (delegated to browser.py, optional)
    L4  manual                        (report honestly, do not force it)

Nothing here requires a third-party package; JSON APIs are read with urllib.
If a service is down or rate-limits, the function returns an empty list and the
caller simply moves to the next tier. Never raise from a source probe.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..core.verify import title_score  # noqa: F401  (re-exported for callers)

UA = ("litfetch/1.0 (+https://example.invalid/litfetch; mailto:{mailto}) "
      "Python-urllib")
DEFAULT_MAILTO = os.environ.get("LITFETCH_MAILTO", "")


def _ua() -> str:
    return UA.format(mailto=DEFAULT_MAILTO or "anonymous")


# Minimum seconds between two requests to the same host. arXiv's API terms ask
# for three; Crossref and Unpaywall ask for a contact address rather than a rate,
# but one per second is the polite default their docs imply. Taken seriously
# because a throttled batch does not fail loudly -- it returns garbage or 403s
# that look like "no open copy".
MIN_INTERVAL = {
    "export.arxiv.org": 3.0,
    "api.crossref.org": 1.0,
    "api.openalex.org": 1.0,
    "api.unpaywall.org": 1.0,
    "api.semanticscholar.org": 1.0,
}
_last_call: dict = {}


# Small memo for the three title searches. A single `resolve()` used to query
# arXiv twice -- once inside `resolve_title`, once for the L0 tier -- and the
# arXiv API asks for three seconds between calls, so the duplicate cost six.
_MEMO: dict = {}


def _cached(key, producer):
    if key in _MEMO:
        return _MEMO[key]
    value = producer()
    if len(_MEMO) > 256:
        _MEMO.clear()
    _MEMO[key] = value
    return value


def _throttle(url: str) -> None:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    gap = MIN_INTERVAL.get(host)
    if not gap:
        return
    wait = gap - (time.time() - _last_call.get(host, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last_call[host] = time.time()


def _retry_after(exc: "urllib.error.HTTPError", cap: float = 30.0) -> float:
    """Honour `Retry-After` when a server sends one, with a sane cap.

    scihub-cli does this and it is the difference between backing off politely
    and hammering a host that has just told you to stop.
    """
    raw = ""
    try:
        raw = (exc.headers or {}).get("Retry-After", "") or ""
    except Exception:
        raw = ""
    if not raw:
        return 0.0
    try:
        return min(float(raw), cap)
    except ValueError:
        # HTTP-date form: fall back to a fixed, short pause rather than parsing.
        return min(5.0, cap)


def http_json(url: str, timeout: float = 12.0, retries: int = 2) -> Optional[dict]:
    delay = 1.0
    for attempt in range(retries):
        _throttle(url)
        req = urllib.request.Request(url, headers={
            "User-Agent": _ua(),
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as fh:
                return json.loads(fh.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 503) and attempt + 1 < retries:
                time.sleep(max(delay, _retry_after(exc)))
                delay *= 2
                continue
            return None
        except Exception:
            if attempt + 1 < retries:
                time.sleep(delay)
                delay *= 2
                continue
            return None
    return None


@dataclass
class Reference:
    """One line of a bibliography, as far as we know it."""

    num: str = ""
    title: str = ""
    year: str = ""
    authors: str = ""
    doi: str = ""
    urls: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Reference":
        return cls(
            num=str(d.get("num", "")),
            title=(d.get("title") or "").strip(),
            year=str(d.get("year") or ""),
            authors=(d.get("authors") or "").strip(),
            doi=(d.get("doi") or "").strip(),
            urls=list(d.get("urls") or []),
        )

    def surname(self) -> str:
        first = re.split(r"[,;]", self.authors)[0].strip()
        return first.split()[0] if first else ""


@dataclass
class Resolved:
    ref: Reference
    canonical_title: str = ""
    year: str = ""
    doi: str = ""
    tiers: Dict[str, List[str]] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def ordered(self) -> List[tuple]:
        out = []
        for tier in ("L0", "L1", "LA", "L2", "manual"):
            for url in self.tiers.get(tier, []):
                out.append((tier, url))
        return out


def _norm_title(t: str) -> str:
    t = re.sub(r"[^\w\s]", " ", t.casefold())
    return re.sub(r"\s+", " ", t).strip()


def crossref_by_doi(doi: str) -> Optional[dict]:
    data = http_json("https://api.crossref.org/works/" + urllib.parse.quote(doi))
    if not data:
        return None
    m = data.get("message", {})
    return {
        "title": (m.get("title") or [""])[0],
        "year": str((m.get("issued", {}).get("date-parts") or [[None]])[0][0] or ""),
        "authors": "; ".join(
            f"{a.get('family','')} {a.get('given','')}".strip()
            for a in m.get("author", [])[:6]
        ),
        "links": [l.get("URL") for l in m.get("link", []) if l.get("URL")],
        "container": (m.get("container-title") or [""])[0],
    }


def crossref_search(title: str, rows: int = 5) -> List[dict]:
    return _cached(("crossref", title, rows), lambda: _crossref_search(title, rows))


def _crossref_search(title: str, rows: int) -> List[dict]:
    q = urllib.parse.quote(title)
    data = http_json(
        "https://api.crossref.org/works"
        f"?query.bibliographic={q}&rows={rows}"
        "&select=title,author,issued,DOI,container-title,link"
    )
    if not data:
        return []
    out = []
    for m in data.get("message", {}).get("items", []):
        out.append({
            "doi": m.get("DOI", ""),
            "title": (m.get("title") or [""])[0],
            "year": str((m.get("issued", {}).get("date-parts") or [[None]])[0][0] or ""),
            "links": [l.get("URL") for l in m.get("link", []) if l.get("URL")],
            "container": (m.get("container-title") or [""])[0],
        })
    return out


def openalex_locations(doi: str) -> List[str]:
    data = http_json("https://api.openalex.org/works/doi:" + urllib.parse.quote(doi))
    if not data:
        return []
    urls = []
    for loc in (data.get("locations") or []):
        if loc.get("is_oa") and loc.get("pdf_url"):
            urls.append(loc["pdf_url"])
    best = (data.get("best_oa_location") or {}).get("pdf_url")
    if best:
        urls.insert(0, best)
    return urls


def unpaywall(doi: str) -> List[str]:
    mailto = DEFAULT_MAILTO
    if not mailto:
        return []
    url = (f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi)}"
           f"?email={urllib.parse.quote(mailto)}")
    data = http_json(url)
    if not data:
        return []
    urls = []
    for loc in (data.get("oa_locations") or []):
        if loc.get("url_for_pdf"):
            urls.append(loc["url_for_pdf"])
    return urls


def arxiv_search(title: str, rows: int = 5) -> List[dict]:
    return _cached(("arxiv", title, rows), lambda: _arxiv_search(title, rows))


def _arxiv_search(title: str, rows: int) -> List[dict]:
    q = urllib.parse.quote(f'ti:"{title}"')
    url = f"https://export.arxiv.org/api/query?search_query={q}&max_results={rows}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _ua()})
        with urllib.request.urlopen(req, timeout=25) as fh:
            xml = fh.read().decode("utf-8", "replace")
    except Exception:
        return []
    out = []
    for entry in re.findall(r"<entry>(.*?)</entry>", xml, re.S):
        def grab(tag: str) -> str:
            m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", entry, re.S)
            return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
        aid = grab("id").rsplit("/", 1)[-1]
        pdf = ""
        for link in re.findall(r'<link[^>]*href="([^"]+)"[^>]*', entry):
            if "/pdf/" in link:
                pdf = link
                break
        out.append({"id": aid, "title": grab("title"), "pdf": pdf or
                    (f"https://arxiv.org/pdf/{aid}" if aid else "")})
    return out


def semanticscholar_pdf(doi: str) -> List[str]:
    """Open-access PDF that Semantic Scholar knows about.

    Often the only index that points at an author's own server rather than at
    the publisher. Free, no key; a key only raises the rate limit.
    """
    if not doi:
        return []
    data = http_json(
        "https://api.semanticscholar.org/graph/v1/paper/DOI:"
        + urllib.parse.quote(doi) + "?fields=openAccessPdf")
    if not data:
        return []
    pdf = (data.get("openAccessPdf") or {}).get("url")
    return [pdf] if pdf else []


def web_search_pdfs(title: str, rows: int = 6, timeout: float = 20.0) -> tuple:
    """Ask a search engine for `"<title>" filetype:pdf`. Returns (urls, note).

    This is the "author's homepage" route in practice: the copy usually sits on a
    university page that no OA index covers. It is also the most fragile route --
    search engines block scripted clients routinely -- so the note says what
    happened instead of pretending the paper does not exist.

    Deliberately no query-string guessing and no scraping of result snippets: we
    only need candidate URLs, and every one of them is verified against the title
    block afterwards. A wrong hit costs one download, not a wrong citation.
    """
    if not title:
        return [], "no title"
    query = urllib.parse.quote(f'"{title}" filetype:pdf')
    url = f"https://html.duckduckgo.com/html/?q={query}"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=timeout) as fh:
            body = fh.read(400_000).decode("utf-8", "replace")
    except Exception as exc:
        return [], f"search unreachable ({type(exc).__name__})"

    if "anomaly" in body.lower() or "unusual traffic" in body.lower():
        return [], "search engine served a bot check"
    urls = []
    for href in re.findall(r'class="result__a"[^>]*href="([^"]+)"', body):
        href = href.replace("&amp;", "&")
        m = re.search(r"[?&]uddg=([^&]+)", href)
        if m:
            href = urllib.parse.unquote(m.group(1))
        if href.startswith("http") and href not in urls:
            urls.append(href)
        if len(urls) >= rows:
            break
    return urls, "" if urls else "no result parsed"


def verify_id(identifier: str, expected_title: str, timeout: float = 20.0) -> tuple:
    """Check that a DOI / arXiv id really points at the paper we think it does.

    2026-10-22: two arXiv ids recalled from memory (1802.05438, 1812.11103)
    pointed at entirely different papers. Ids are cheap to check and expensive
    to trust, so every id that arrives from outside is checked here, before a
    download is attempted.
    """
    ident = identifier.strip()
    actual = ""
    if re.match(r"^\d{4}\.\d{4,5}(v\d+)?$", ident):          # arXiv
        data = http_json(f"https://export.arxiv.org/api/query?id_list={ident}",
                         timeout=timeout)
        if data is None:
            xml_url = f"https://export.arxiv.org/api/query?id_list={ident}"
            try:
                req = urllib.request.Request(xml_url, headers={"User-Agent": _ua()})
                with urllib.request.urlopen(req, timeout=timeout) as fh:
                    xml = fh.read().decode("utf-8", "replace")
                m = re.search(r"<entry>.*?<title>(.*?)</title>", xml, re.S)
                if m:
                    actual = re.sub(r"\s+", " ", m.group(1)).strip()
            except Exception:
                return False, "", 0.0
    else:                                                       # DOI
        meta = crossref_by_doi(ident)
        if meta:
            actual = meta.get("title", "")
    if not actual:
        return False, "", 0.0
    ok, ratio = titles_match(expected_title, actual)
    return ok, actual, ratio


def titles_match(expected: str, actual: str) -> tuple:
    """Loose comparison used only to sanity-check an identifier.

    Deliberately looser than `verify.verify_pdf`: this runs *before* the download
    and only has to catch "this id points at a different paper", not to certify
    the file. The strict check happens on the bytes.
    """
    want, got = _norm_title(expected), _norm_title(actual)
    if not want:
        return True, 1.0
    if want in got or got in want:
        return True, 1.0
    ratio = len(set(want.split()) & set(got.split())) / max(1, len(want.split()))
    return ratio >= 0.8, round(ratio, 3)


def openalex_search(title: str, rows: int = 5) -> List[dict]:
    """Title search against OpenAlex. Free, no key, and it indexes DOIs Crossref lacks."""
    return _cached(("openalex", title, rows), lambda: _openalex_search(title, rows))


def _openalex_search(title: str, rows: int) -> List[dict]:
    q = urllib.parse.quote(title)
    data = http_json(f"https://api.openalex.org/works?search={q}&per-page={rows}")
    if not data:
        return []
    out = []
    for w in data.get("results", []):
        doi = (w.get("doi") or "").replace("https://doi.org/", "")
        out.append({
            "doi": doi,
            "title": w.get("title") or w.get("display_name") or "",
            "year": str(w.get("publication_year") or ""),
            "links": [],
            "container": ((w.get("primary_location") or {}).get("source") or {}).get("display_name", ""),
        })
    return out


def resolve_title(query: str, rows: int = 5, min_score: float = 0.9) -> List[dict]:
    """Rank title candidates from Crossref, OpenAlex and arXiv, with scores.

    Returns every candidate, best first, each carrying `score`, `sources` and
    `accepted`. **Callers must not use a rejected candidate.** Returning the
    rejected ones is the point: a report can then say *why* a title did not
    resolve ("best 0.61: 'Online Relational Inference ...'"), which is a finding,
    whereas an empty list looks like a network failure.
    """
    pool: Dict[str, dict] = {}

    def offer(source: str, hit: dict) -> None:
        title = (hit.get("title") or "").strip()
        if not title:
            return
        key = _norm_title(title)
        score = title_score(query, title)
        rec = pool.get(key)
        if rec is None:
            rec = pool[key] = {"title": title, "doi": hit.get("doi", ""),
                               "year": hit.get("year", ""), "links": [],
                               "container": hit.get("container", ""),
                               "score": score, "sources": set()}
        rec["sources"].add(source)
        if not rec["doi"] and hit.get("doi"):
            rec["doi"] = hit["doi"]
        for link in hit.get("links") or []:
            if link and link not in rec["links"]:
                rec["links"].append(link)

    for hit in crossref_search(query, rows=rows):
        offer("crossref", hit)
    for hit in openalex_search(query, rows=rows):
        offer("openalex", hit)
    for hit in arxiv_search(query, rows=rows):
        offer("arxiv", {"title": hit.get("title", ""), "doi": "",
                        "year": "", "links": [hit.get("pdf", "")]})

    out = sorted(pool.values(),
                 key=lambda r: (-r["score"], len(r["title"]), -len(r["sources"])))
    for rec in out:
        rec["sources"] = sorted(rec["sources"])
        rec["accepted"] = rec["score"] >= min_score
    return out


def resolve(ref: Reference, use_arxiv: bool = True) -> Resolved:
    """Fill in canonical metadata and the per-tier candidate URL list."""
    res = Resolved(ref=ref, canonical_title=ref.title, year=ref.year, doi=ref.doi)
    # LA is "author page / arbitrary web": URLs a human or an agent found and
    # supplied. Tried *before* doi.org, because for a paywalled paper the author's
    # own copy is more likely to be reachable than the publisher.
    tiers: Dict[str, List[str]] = {"L0": [], "L1": [], "LA": list(ref.urls), "L2": []}

    if not res.doi and ref.title:
        ranked = resolve_title(ref.title)
        accepted = [c for c in ranked if c["accepted"] and c["doi"]]
        if accepted:
            hit = accepted[0]
            res.doi = hit["doi"]
            res.canonical_title = hit["title"] or ref.title
            res.year = res.year or hit["year"]
            tiers["L1"].extend(hit.get("links") or [])
            if len(hit["sources"]) > 1:
                res.notes.append("DOI agreed by " + "+".join(hit["sources"]))
        elif ranked:
            top = ranked[0]
            res.notes.append(
                f'no confident DOI (best {top["score"]:.2f}: "{top["title"][:60]}")')

    if res.doi:
        meta = crossref_by_doi(res.doi)
        if meta:
            res.canonical_title = meta["title"] or res.canonical_title
            res.year = res.year or meta["year"]
            tiers["L1"].extend(meta.get("links") or [])
        tiers["L1"].extend(unpaywall(res.doi))
        tiers["L1"].extend(openalex_locations(res.doi))
        tiers["L2"].append(f"https://doi.org/{res.doi}")

    if use_arxiv and ref.title:
        for hit in arxiv_search(ref.title):
            if title_score(ref.title, hit["title"]) >= 0.9:
                tiers["L0"].append(hit["pdf"])
                break

    # Dedupe across tiers as well as within: the same arXiv PDF arrives from
    # Crossref's link list and from OpenAlex's locations, and a URL already
    # tried at L0 must not be tried again at L1.
    seen: set = set()
    for k in ("L0", "L1", "LA", "L2", "manual"):
        uniq = []
        for u in tiers.get(k, []):
            if u and u not in seen:
                seen.add(u)
                uniq.append(u)
        tiers[k] = uniq
    res.tiers = tiers
    return res
