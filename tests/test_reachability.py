"""Reachability classification is static and must stay correct without network.

These rules were each forced by a real failure in the CrossScale R1-R12 batch.
They are pinned here so a future refactor cannot silently reintroduce:
  * the PMLR ``.html`` suffix bug (produced ``orujlu26a.html.pdf`` -> 404)
  * PMC/MDPI being filed as subscription walls when they are OA-but-browser-only
  * Cambridge AOP (open access) being lumped in with paywalled Cambridge DOIs
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from litfetch.io import reachability as R  # noqa: E402


def test_arxiv_is_auto_with_both_hosts():
    v = R.classify("C022", "Some paper", ["https://arxiv.org/abs/2608.03076"])
    assert v.verdict == R.AUTO
    urls = [c.url for c in v.candidates]
    assert "https://arxiv.org/pdf/2608.03076" in urls
    assert "https://export.arxiv.org/pdf/2608.03076" in urls


def test_pmlr_strips_html_suffix_regression():
    """The landing `<id>.html` must NOT leak into the PDF filename."""
    v = R.url_verdict("https://proceedings.mlr.press/v337/orujlu26a.html")
    assert v.verdict == R.AUTO
    joined = " ".join(c.url for c in v.candidates)
    assert "orujlu26a/orujlu26a.pdf" in joined      # correct mlresearch path
    assert ".html.pdf" not in joined                 # the old bug
    assert "orujlu26a.html" not in joined


def test_cvf_and_iclr_are_auto_scrape():
    for url in (
        "https://openaccess.thecvf.com/content/CVPR2026/papers/X_CVPR_2026_paper.pdf",
        "https://proceedings.iclr.cc/paper_files/paper/2024/hash/abc-Abstract-Conference.html",
    ):
        assert R.url_verdict(url).verdict == R.AUTO


def test_cambridge_aop_is_auto_not_paywalled():
    v = R.url_verdict(
        "https://www.cambridge.org/core/services/aop-cambridge-core/content/view/ABC")
    assert v.verdict == R.AUTO, "Cambridge AOP is reachable from the sandbox"


def test_cambridge_journal_landing_is_not_misreported_as_subscription():
    """A journal article page may have an OA AOP copy; do not send it to the office."""
    v = R.url_verdict(
        "https://www.cambridge.org/core/journals/acta-numerica/article/"
        "optimal-experimental-design-formulations-and-computations/ABC")
    assert v.verdict == R.AUTO
    assert v.verdict != R.OFFICE_SUB


def test_pmc_is_browser_only_not_subscription():
    v = R.url_verdict("https://pmc.ncbi.nlm.nih.gov/articles/PMC11494085/")
    assert v.verdict == R.BROWSER
    assert any("PMC11494085/pdf/" in c.url for c in v.candidates)


def test_pubmed_is_browser_only():
    assert R.url_verdict("https://pubmed.ncbi.nlm.nih.gov/40424147/").verdict == R.BROWSER


def test_mdpi_is_browser_not_subscription():
    """MDPI is open access; 403 to curl here is an anti-bot wall, not a paywall."""
    v = R.url_verdict("https://www.mdpi.com/1099-4300/27/8/825")
    assert v.verdict == R.BROWSER
    assert v.verdict != R.OFFICE_SUB


def test_subscription_dois_are_office():
    for url in (
        "https://doi.org/10.1016/j.patter.2025.101472",
        "https://doi.org/10.1038/s41586-026-10489-5",
        "https://www.sciencedirect.com/science/article/pii/123",
    ):
        assert R.url_verdict(url).verdict == R.OFFICE_SUB, url


def test_github_is_repo_not_a_pdf():
    assert R.url_verdict("https://github.com/foo/bar").verdict == R.REPO


def test_aggregate_prefers_auto_over_subscription():
    """A ref with both a DOI and an arXiv copy: AUTO wins (that is the fetchable path)."""
    v = R.classify("X", "T",
                   ["https://doi.org/10.9999/x"], doi="")
    assert v.verdict == R.OFFICE_SUB
    v2 = R.classify("X", "T",
                    ["https://doi.org/10.9999/x", "https://arxiv.org/abs/2608.03076"])
    assert v2.verdict == R.AUTO


def test_broken_pdf_bytes_rejected():
    if R.pymupdf is None:
        # Documented degradation: with no PDF backend the check is magic bytes
        # and size only, so "%PDF-1.4 garbage" is indistinguishable from a paper.
        # Asserting rejection here would demand a backend this job deliberately
        # does not install.
        from _support import Skip
        raise Skip("needs a PDF backend (pymupdf)")
    ok, pages = R._valid_pdf_bytes(b"%PDF-1.4 not really a document")
    assert ok is False and pages == 0


def test_classify_emits_plan_fields():
    d = R.classify("C011", "PMC paper",
                   ["https://pmc.ncbi.nlm.nih.gov/articles/PMC11494085/"]).as_dict()
    for key in ("num", "title", "link", "verdict", "note", "candidates"):
        assert key in d


# --- campus-network findings (2026-11-02) ---------------------------------

def test_cloudflare_challenge_is_recognised_as_bot_detection():
    cf = b"<html><head><title>Just a moment...</title></head><body>cf-challenge</body></html>"
    assert R.is_cloudflare_challenge(cf) is True
    assert R.is_cloudflare_challenge(b"<!doctype html><title>Access denied</title>") is False
    assert R.is_cloudflare_challenge(b"%PDF-1.7 real paper") is False
    assert R.is_cloudflare_challenge(b"") is False


def test_oa_copy_verdict_exists_and_discover_oa_is_safe_without_doi():
    assert R.OA_COPY == "OA_COPY"
    d = R.discover_oa("")            # no DOI -> no network, empty result
    assert d["oa_pdf"] is None and d["arxiv_pdf"] is None
    assert d["declared_title"] == ""


def test_plain_classify_stays_static_when_oa_lookup_off():
    """The default plan must remain offline-reproducible; OA discovery is opt-in."""
    v = R.classify("C003", "A paywalled paper",
                   ["https://doi.org/10.1016/j.patter.2025.101472"],
                   doi="10.1016/j.patter.2025.101472", oa_lookup=False)
    assert v.verdict == R.OFFICE_SUB
    assert v.candidates == []


def test_doi_is_extracted_from_the_link_even_without_a_doi_column():
    """CSV rows carry the DOI inside the link; the CLI must pass it to OA lookup."""
    link = "https://doi.org/10.1073/pnas.2519631123"
    doi = link.split("doi.org/", 1)[-1]
    assert doi == "10.1073/pnas.2519631123"
