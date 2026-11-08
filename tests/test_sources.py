"""Title -> DOI resolution must refuse a near miss, not rank it first.

The code this replaces compared `normalise(title)[:40]`, which is exactly the
shape that accepts a sequel: "Online Relational Inference for Evolving
Multi-agent Interacting Systems" agrees with "Neural Relational Inference for
Interacting Systems" for every character a prefix test looks at, and shares five
of six content words. DoiHunter resolves the same problem with a threshold
(`--minimum-title-score`, default 0.92); this module borrows the idea and the
score, not the number.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from litfetch.io import sources  # noqa: E402

TITLE = "Neural Relational Inference for Interacting Systems"
SEQUEL = "Online Relational Inference for Evolving Multi-agent Interacting Systems"


def test_exact_title_scores_one():
    assert sources.title_score(TITLE, TITLE) == 1.0


def test_a_subtitle_does_not_cost_anything():
    assert sources.title_score("Deep Sets", "Deep Sets: Theory and Practice") == 1.0


def test_sequel_scores_below_the_threshold():
    score = sources.title_score(TITLE, SEQUEL)
    assert score < 0.9, score
    assert score > 0.3      # and it is not mistaken for an unrelated paper


def test_unrelated_title_scores_near_zero():
    assert sources.title_score(TITLE, "A Sparsity-Based Model of Bounded Rationality") < 0.2


def test_resolve_title_accepts_only_the_real_paper(monkeypatch):
    monkeypatch.setattr(sources, "crossref_search", lambda q, rows=5: [
        {"doi": "10.9999/sequel", "title": SEQUEL, "year": "2024", "links": []},
    ])
    monkeypatch.setattr(sources, "openalex_search", lambda q, rows=5: [
        {"doi": "10.9999/real", "title": TITLE, "year": "2018", "links": []},
    ])
    monkeypatch.setattr(sources, "arxiv_search", lambda q, rows=5: [
        {"id": "1802.04687", "title": TITLE, "pdf": "https://arxiv.org/pdf/1802.04687"},
    ])

    ranked = sources.resolve_title(TITLE)
    assert ranked[0]["doi"] == "10.9999/real"
    assert ranked[0]["accepted"]
    assert ranked[0]["sources"] == ["arxiv", "openalex"]   # two sources agreed
    assert not any(c["accepted"] for c in ranked if c["title"] == SEQUEL)


def test_resolve_title_reports_rejected_candidates_rather_than_nothing(monkeypatch):
    """An empty list looks like a network failure; a rejected list is a finding."""
    monkeypatch.setattr(sources, "crossref_search", lambda q, rows=5: [
        {"doi": "10.9999/sequel", "title": SEQUEL, "year": "2024", "links": []}])
    monkeypatch.setattr(sources, "openalex_search", lambda q, rows=5: [])
    monkeypatch.setattr(sources, "arxiv_search", lambda q, rows=5: [])

    ranked = sources.resolve_title(TITLE)
    assert len(ranked) == 1
    assert ranked[0]["accepted"] is False


def test_resolve_does_not_guess_a_doi_from_a_weak_match(monkeypatch):
    monkeypatch.setattr(sources, "crossref_search", lambda q, rows=5: [
        {"doi": "10.9999/sequel", "title": SEQUEL, "year": "2024", "links": []}])
    monkeypatch.setattr(sources, "openalex_search", lambda q, rows=5: [])
    monkeypatch.setattr(sources, "arxiv_search", lambda q, rows=5: [])
    monkeypatch.setattr(sources, "crossref_by_doi", lambda d: None)

    ref = sources.Reference(num="X", title=TITLE, year="2018")
    res = sources.resolve(ref)
    assert res.doi == ""
    assert any("no confident DOI" in n for n in res.notes)


def test_a_prepended_content_word_means_a_different_paper():
    """Found on a live query: "Conditional neural relational inference for
    interacting systems" scored 1.00 under the naive verbatim rule."""
    score = sources.title_score(TITLE, "Conditional " + TITLE[0].lower() + TITLE[1:])
    assert score < 0.9, score


def test_a_leading_article_does_not_change_identity():
    assert sources.title_score("sparsity-based model of bounded rationality",
                               "A sparsity-based model of bounded rationality") == 1.0


def test_supplied_urls_are_tried_before_the_publisher():
    """An author's own PDF is more likely to be reachable than doi.org.

    R143 (Fan 1991) exists on the author's own archive and behind an Incapsula
    wall at Project Euclid; the order decides whether the reference is fetched.
    """
    ref = sources.Reference(num="R143", title="T",
                            urls=["https://archive.example/fan1991.pdf"])
    res = sources.Resolved(ref=ref, tiers={"L0": [], "L1": ["https://oa.example/x.pdf"],
                                           "LA": list(ref.urls),
                                           "L2": ["https://doi.org/10.1/x"]})
    order = [tier for tier, _ in res.ordered()]
    assert order == ["L1", "LA", "L2"]


def test_semanticscholar_points_at_the_pdf(monkeypatch):
    monkeypatch.setattr(sources, "http_json",
                        lambda url, **kw: {"openAccessPdf": {"url": "https://a.example/p.pdf"}})
    assert sources.semanticscholar_pdf("10.1/x") == ["https://a.example/p.pdf"]


def test_semanticscholar_without_oa_returns_nothing(monkeypatch):
    monkeypatch.setattr(sources, "http_json", lambda url, **kw: {"openAccessPdf": None})
    assert sources.semanticscholar_pdf("10.1/x") == []
    assert sources.semanticscholar_pdf("") == []


def test_web_search_reports_why_it_could_not_search(monkeypatch):
    """A blocked search is a fact to report, not an empty result to hide."""
    def boom(*a, **kw):
        raise OSError("tunnel failed")
    monkeypatch.setattr(sources.urllib.request, "urlopen", boom)
    urls, note = sources.web_search_pdfs("Some Title")
    assert urls == []
    assert "unreachable" in note


def test_web_search_needs_a_title():
    urls, note = sources.web_search_pdfs("")
    assert urls == [] and note == "no title"
