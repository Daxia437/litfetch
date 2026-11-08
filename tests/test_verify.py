import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:                                  # the runner turns ImportError into a skip
    import pymupdf
except ImportError:                   # pragma: no cover
    raise ImportError("pymupdf is required for the PDF-based tests")

from litfetch.core.verify import (  # noqa: E402
    coverage, normalise, sniffs, title_tokens, verify_pdf,
)

TITLE = "Formal Error Bounds for the State Space Reduction of Markov Chains"
MIN = 1000   # synthetic PDFs are small; the real default stays 5000


def make_pdf(path: Path, first_page_text: str, pages: int = 2) -> Path:
    """A real, parseable PDF. insert_textbox (not insert_text) so the text wraps
    instead of running off the page -- a clipped page passes no text check."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(72, 72, 520, 760), first_page_text[:1800], fontsize=9)
    for i in range(pages - 1):
        doc.new_page().insert_textbox(pymupdf.Rect(72, 72, 520, 760),
                                      "continuation " * 20, fontsize=9)
    doc.save(str(path))
    doc.close()
    return path


def test_normalise_exandles_ligatures_and_spacing_accents():
    assert "identifiability" in normalise("identiﬁability")
    assert normalise("Hyv¨arinen") == "hyvarinen"
    assert normalise("Naïve—Bayes") == "naive bayes"


def test_title_tokens_drop_stopwords_and_short_words():
    toks = title_tokens("On the Optimal Rates of Convergence for a Test")
    assert "the" not in toks and "for" not in toks
    assert "optimal" in toks


def test_coverage_partial():
    text = "Formal Error Bounds for the State Space Reduction of Markov Chains"
    assert coverage(TITLE, text) == 1.0
    half = "Formal Error Bounds for something else entirely"
    assert 0.2 < coverage(TITLE, half) < 0.8


def test_sniffs_html_vs_pdf(tmp_path):
    h = tmp_path / "landing.pdf"
    h.write_bytes(b"<!DOCTYPE html><html><body>ScienceDirect</body></html>")
    assert sniffs(h) == "html"

    stub = tmp_path / "stub.pdf"
    stub.write_bytes(b"")
    assert sniffs(stub) == "empty"


def test_verify_accepts_the_right_paper(tmp_path):
    p = make_pdf(tmp_path / "right.pdf", TITLE + "\nIgnacio Esponda, Demian Pouzo\n2024")
    v = verify_pdf(p, expected_title=TITLE, expected_year="2024", min_bytes=MIN)
    assert v.ok, v.reasons
    assert v.pages == 2
    assert v.coverage > 0.9


def test_verify_rejects_a_different_paper(tmp_path):
    wrong = ("Online Relational Inference for Evolving Multi-agent Interacting Systems. "
             "Kang, Saha, Sharma, Chakraborty, Mukhopadhyay. Georgia Institute of Technology. "
             "We introduce a novel framework designed to identify hidden interaction graphs "
             "in evolving multi-agent systems from streaming data.")
    p = make_pdf(tmp_path / "wrong.pdf", wrong)
    v = verify_pdf(p, expected_title=TITLE, min_bytes=MIN)
    assert not v.ok
    assert any("coverage" in r for r in v.reasons)


def test_verify_rejects_landing_page(tmp_path):
    p = tmp_path / "landing.pdf"
    p.write_bytes(b"<html>" + b"x" * 8000 + b"</html>")
    v = verify_pdf(p, expected_title=TITLE, min_bytes=MIN)
    assert not v.ok
    assert v.kind == "html"


def test_short_title_uses_phrase_not_tokens(tmp_path):
    p = make_pdf(tmp_path / "short.pdf", "Deep Sets\nManzil Zaheer, 2017")
    assert verify_pdf(p, expected_title="Deep Sets", min_bytes=MIN).ok
    assert not verify_pdf(p, expected_title="Deep Nets", min_bytes=MIN).ok


def test_longest_run_rejects_a_sequel_with_the_same_words():
    from litfetch.core.verify import longest_run

    sequel = ("Online Relational Inference for Evolving Multi-agent Interacting Systems. "
              "Kang et al. We propose ORI, a framework for hidden interaction graphs.")
    assert coverage("Neural Relational Inference for Interacting Systems", sequel) >= 0.8
    assert longest_run("Neural Relational Inference for Interacting Systems", sequel) <= 3


def test_verify_rejects_a_sequel_of_the_right_paper(tmp_path):
    """The failure mode that a surname check and a word-bag check both miss."""
    sequel = ("Online Relational Inference for Evolving Multi-agent Interacting Systems. "
              "Kang, Saha, Sharma, Chakraborty, Mukhopadhyay. Georgia Institute of Technology. "
              "We introduce a framework for identifying hidden interaction graphs from streams.")
    p = make_pdf(tmp_path / "sequel.pdf", sequel)
    v = verify_pdf(p, expected_title="Neural Relational Inference for Interacting Systems",
                   min_bytes=MIN)
    assert not v.ok
    assert any("front matter" in r for r in v.reasons)


def test_import_dir_matches_and_refuses_to_guess(tmp_path):
    """A folder of hand downloads: one file belongs to R1, one belongs to nothing."""
    from litfetch.io import sources
    from litfetch.core.store import Library

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    make_pdf(inbox / "download_1.pdf",
             "Formal Error Bounds for the State Space Reduction of Markov Chains "
             "Michel, Siegle. 2024. We bound the aggregation error of a lumped chain.")
    make_pdf(inbox / "download_2.pdf",
             "Online Relational Inference for Evolving Multi-agent Interacting Systems "
             "Kang, Saha, Sharma. We propose ORI for streaming interaction graphs.")

    refs = [sources.Reference(num="R1",
                              title="Formal Error Bounds for the State Space Reduction of Markov Chains",
                              year="2024", authors="Michel, Siegle"),
            sources.Reference(num="R2",
                              title="A Fourier Approach to Nonparametric Deconvolution of a Density Estimate",
                              year="1993", authors="Diggle, Hall")]
    lib = Library(tmp_path / "lib")
    results = lib.import_dir(inbox, refs, layer="L14", copy=True, min_bytes=MIN)

    by_name = {r.source.name: r for r in results}
    assert by_name["download_1.pdf"].status == "stored"
    assert by_name["download_1.pdf"].path.name.startswith("Michel_2024_")
    assert by_name["download_2.pdf"].status == "unmatched"      # never guesses
    assert len(list((tmp_path / "lib").rglob("*.pdf"))) == 1


def test_a_missing_year_is_a_warning_when_the_title_is_exact(tmp_path):
    """An old offprint may print the title and the journal but no year."""
    p = make_pdf(tmp_path / "offprint.pdf",
                 "Formal Error Bounds for the State Space Reduction of Markov Chains "
                 "Michel, Siegle. We bound the aggregation error of a lumped chain.")
    v = verify_pdf(p, expected_title="Formal Error Bounds for the State Space Reduction of Markov Chains",
                   expected_year="2024", min_bytes=MIN)
    assert v.ok
    assert v.warnings and "2024" in v.warnings[0]


def test_a_contradicting_year_still_rejects_an_exact_title(tmp_path):
    """The failure that motivated the rule: a 1988 technical report of a 1991 paper.

    Same title, same author, different document -- and an exact title match is
    exactly what makes the substitution invisible.
    """
    p = make_pdf(tmp_path / "tr.pdf",
                 "Formal Error Bounds for the State Space Reduction of Markov Chains "
                 "Technical Report No. 157, June 1988. We bound the aggregation error.")
    v = verify_pdf(p, expected_title="Formal Error Bounds for the State Space Reduction of Markov Chains",
                   expected_year="1991", min_bytes=MIN)
    assert not v.ok
    assert any("different version" in r for r in v.reasons)


def test_a_letter_spaced_title_is_still_the_same_paper(tmp_path):
    """Old journals set titles as spaced capitals: 'F O R M A L  E R R O R'.

    Every word-level test dies on that -- the page has no token 'formal', only
    f|o|r|m|a|l -- so the whole title must be matched with spaces removed.
    Found on Kolmogorov 1965 (Проблемы передачи информации), which the audit
    reported as a mismatch until this case existed. Latin here because the
    synthetic PDF's built-in font carries no Cyrillic; the jamming is the point.
    """
    spaced = " ".join("FORMAL ERROR BOUNDS FOR THE STATE SPACE REDUCTION OF MARKOV CHAINS")
    p = make_pdf(tmp_path / "spaced.pdf",
                 spaced + " Michel, Siegle. 2024. We bound the aggregation error "
                 "of a lumped chain and compare it with the exact value.")
    v = verify_pdf(p, expected_title=TITLE, expected_year="2024", min_bytes=MIN)
    assert v.ok, v.reasons
    assert any("substring" in w for w in v.warnings)


def test_the_squashed_fallback_does_not_excuse_a_sequel():
    """The loosening must not swallow the one case the whole checker exists for."""
    from litfetch.core.verify import squashed_contains
    sequel = ("Online Relational Inference for Evolving Multi-agent Interacting Systems")
    assert not squashed_contains("Neural Relational Inference for Interacting Systems", sequel)
