import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from litfetch.io import fetch, sources  # noqa: E402
from litfetch.core.store import Library  # noqa: E402


def test_browser_only_run_does_not_early_return(tmp_path):
    """Regression: filtering to L3 emptied the tier list and returned
    "no candidate URL" before the browser tier was ever consulted."""
    original = fetch.sources.resolve
    try:
        fetch.sources.resolve = lambda ref, use_arxiv=True: sources.Resolved(
            ref=ref, canonical_title=ref.title, year=ref.year, doi=ref.doi,
            tiers={"L0": ["https://example.invalid/a.pdf"], "L1": [], "L2": [], "manual": []},
        )
        out = fetch.fetch_ref(
            sources.Reference(num="X", title="Some Paper", year="2020"),
            Library(tmp_path), use_browser=False, tiers_allowed=["L3"],
        )
        assert "browser tier was not enabled" in out.note
        assert out.status == "unavailable"
    finally:
        fetch.sources.resolve = original


def test_id_mismatch_is_reported_before_downloading():
    ok, ratio = sources.titles_match(
        "QMIX: Monotonic Value Function Factorisation for Deep Multi-Agent Reinforcement Learning",
        "Mean Field Multi-Agent Reinforcement Learning",
    )
    assert not ok and ratio < 0.8
    ok2, _ = sources.titles_match("Deep Sets", "Deep Sets")
    assert ok2


# --------------------------------------------------------------------------
# Fail-fast, transport, version filter, and the stored/duplicate distinction.
# --------------------------------------------------------------------------

import pymupdf  # noqa: E402


def _pdf(path, first_page_text):
    doc = pymupdf.open()
    doc.new_page().insert_textbox(pymupdf.Rect(72, 72, 520, 760),
                                  first_page_text[:1800], fontsize=9)
    for _ in range(2):
        doc.new_page().insert_textbox(pymupdf.Rect(72, 72, 520, 760),
                                      "continuation " * 20, fontsize=9)
    doc.save(str(path))
    doc.close()
    return path


def _resolve_to(monkeypatch, tiers):
    original = sources.resolve
    monkeypatch.setattr(fetch.sources, "resolve",
                        lambda ref, use_arxiv=True: sources.Resolved(
                            ref=ref, canonical_title=ref.title, year=ref.year,
                            doi=ref.doi, tiers=tiers))
    return original


def test_bot_wall_stops_the_rest_of_that_host(tmp_path, monkeypatch):
    """Seven silent minutes once came from retrying five URLs on two hosts."""
    calls = []

    def fake_download(url, dest, timeout=60.0, transport="plain"):
        calls.append(url)
        if "a.example" in url:
            body = b"<html><title>Just a moment...</title>checking your browser</html>"
            return fetch.Response(False, 403, len(body), "HTTP 403", "text/html", body, url)
        _pdf(dest, "Formal Error Bounds for the State Space Reduction of Markov Chains. "
                   "Michel and Siegle, 2024. We bound the aggregation error of a lumped chain.")
        return fetch.Response(True, 200, dest.stat().st_size, "", "application/pdf", b"%PDF-1.7", url)

    _resolve_to(monkeypatch, {"L1": ["https://a.example/one.pdf",
                                     "https://a.example/two.pdf"],
                              "L2": ["https://b.example/three.pdf"]})
    monkeypatch.setattr(fetch, "download", fake_download)

    out = fetch.fetch_ref(sources.Reference(num="R1",
                                            title="Formal Error Bounds for the State Space Reduction of Markov Chains",
                                            year="2024", authors="Michel, Siegle"),
                          Library(tmp_path / "lib"), allow_tls=False, min_bytes=1000)
    assert out.status == "stored", out.note
    assert out.level == "L2"
    assert "https://a.example/one.pdf" in calls
    assert "https://a.example/two.pdf" not in calls      # the whole point
    assert "https://b.example/three.pdf" in calls


def test_all_candidates_blocked_reports_blocked_not_rejected(tmp_path, monkeypatch):
    def fake_download(url, dest, timeout=60.0, transport="plain"):
        body = b"<html><title>Just a moment...</title></html>"
        return fetch.Response(False, 403, len(body), "HTTP 403", "text/html", body, url)

    _resolve_to(monkeypatch, {"L1": ["https://a.example/one.pdf"],
                              "L2": ["https://a.example/two.pdf"]})
    monkeypatch.setattr(fetch, "download", fake_download)

    out = fetch.fetch_ref(sources.Reference(num="R2", title="A Paper", year="2020"),
                          Library(tmp_path / "lib"), allow_tls=False, min_bytes=1000)
    assert out.status == "blocked"
    assert out.blocked == "cloudflare"
    assert "browser" in " ".join(fetch.diagnose.ACTION.values()).lower()


def test_version_required_refuses_a_preprint(tmp_path, monkeypatch):
    def fake_download(url, dest, timeout=60.0, transport="plain"):
        _pdf(dest, "Deep Sets. arXiv:1703.06114v1 [cs.LG] 17 Mar 2017. "
                   "2017. We study permutation-invariant functions on sets.")
        return fetch.Response(True, 200, dest.stat().st_size, "", "application/pdf", b"%PDF-1.7", url)

    _resolve_to(monkeypatch, {"L1": ["https://x.example/paper.pdf"]})
    monkeypatch.setattr(fetch, "download", fake_download)

    out = fetch.fetch_ref(sources.Reference(num="R3", title="Deep Sets", year="2017"),
                          Library(tmp_path / "lib"), allow_tls=False, min_bytes=1000,
                          version_required="vor")
    assert out.status == "rejected"
    assert "preprint" in out.note
    assert not list((tmp_path / "lib").rglob("*.pdf"))


def test_second_identical_download_is_reported_as_duplicate(tmp_path, monkeypatch):
    """A batch once printed "stored 4/4" over files that were already there.

    The payload is written from one master file on purpose: two *freshly
    generated* PDFs differ by an embedded creation timestamp, so a byte-hash
    dedupe would miss them. That is a real limitation of content hashing, and the
    test would be lying if it hid it.
    """
    master = _pdf(tmp_path / "master.pdf",
                  "Deep Sets. Zaheer et al., 2017. "
                  "We study permutation-invariant functions on sets.")

    def fake_download(url, dest, timeout=60.0, transport="plain"):
        dest.write_bytes(master.read_bytes())
        return fetch.Response(True, 200, dest.stat().st_size, "", "application/pdf", b"%PDF-1.7", url)

    _resolve_to(monkeypatch, {"L1": ["https://x.example/paper.pdf"]})
    monkeypatch.setattr(fetch, "download", fake_download)
    ref = sources.Reference(num="R4", title="Deep Sets", year="2017", authors="Zaheer")

    lib = Library(tmp_path / "lib")
    first = fetch.fetch_ref(ref, lib, allow_tls=False, min_bytes=1000)
    second = fetch.fetch_ref(ref, lib, allow_tls=False, min_bytes=1000)
    assert first.status == "stored"
    assert second.status == "duplicate"
    assert second.path == first.path
    assert len(list((tmp_path / "lib").rglob("*.pdf"))) == 1


def test_domain_groups_hosts_that_share_a_bot_wall():
    assert fetch.domain("https://academic.oup.com/jrsssb/article-pdf/1/x.pdf") == "oup.com"
    assert fetch.domain("https://www.cambridge.org/core/journals/x") == "cambridge.org"
    assert fetch.domain("https://journals.sagepub.com/doi/10.1") == "sagepub.com"
    assert fetch.domain("https://foo.bar.ac.uk/x.pdf") == "bar.ac.uk"
    assert fetch.domain("http://127.0.0.1:8080/x.pdf") == "127.0.0.1"


def test_verified_refs_reads_only_verified_events(tmp_path):
    log = tmp_path / "attempts.jsonl"
    log.write_text(
        '{"ref": "R1", "result": "verified"}\n'
        '{"ref": "R2", "result": "rejected"}\n'
        '{"ref": "R3", "result": "blocked"}\n'
        '{"ref": "R4", "result": "verified"}\n', encoding="utf-8")
    assert fetch.verified_refs(str(log)) == {"R1", "R4"}
    assert fetch.verified_refs(str(tmp_path / "missing.jsonl")) == set()


def test_block_on_a_redirect_target_skips_the_publisher_url(tmp_path, monkeypatch):
    """doi.org forwards into the publisher; the block belongs to the target.

    Without this, `https://doi.org/10.1/x` fails first (blocking doi.org) and the
    next candidate `https://onlinelibrary.wiley.com/...` walks into the same wall.
    """
    calls = []

    def fake_download(url, dest, timeout=60.0, transport="plain"):
        calls.append(url)
        body = b"<html><title>Just a moment...</title></html>"
        return fetch.Response(False, 403, len(body), "HTTP 403", "text/html", body,
                              "https://onlinelibrary.wiley.com/doi/pdf/10.1/x")

    _resolve_to(monkeypatch, {"L2": ["https://doi.org/10.1/x",
                                     "https://onlinelibrary.wiley.com/doi/pdf/10.1/x"]})
    monkeypatch.setattr(fetch, "download", fake_download)

    out = fetch.fetch_ref(sources.Reference(num="R5", title="A Paper", year="2020"),
                          Library(tmp_path / "lib"), allow_tls=False)
    assert out.status == "blocked"
    assert len(calls) == 1                      # the publisher URL was skipped


def test_tls_retry_is_not_wasted_on_a_full_html_page_or_a_400():
    """Both were measured: 784 KB pages and HTTP 400s were retried for nothing."""
    assert fetch.worth_tls_retry("js_required", 200, 784_033, "text/html") is False
    assert fetch.worth_tls_retry("not_pdf", 400, 0, "text/plain") is False
    assert fetch.worth_tls_retry("not_pdf", 403, 0, "") is True
    assert fetch.worth_tls_retry("js_required", 502, 1200, "text/html") is True
    assert fetch.worth_tls_retry("not_pdf", 200, 46_174, "text/html") is False
    assert fetch.worth_tls_retry("cloudflare", 403, 5905, "text/html") is False
