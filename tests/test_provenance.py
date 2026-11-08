"""Provenance: identity is not version.

The case that motivated this module: R141 was fetched from arXiv and stored under
the name of the journal article. Four conditions passed -- it is the right paper
-- and nothing in the library said the bytes are a preprint.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from litfetch.core import provenance  # noqa: E402


def test_arxiv_host_means_preprint():
    v, why = provenance.classify(url="https://arxiv.org/pdf/2403.07618v3", text="")
    assert v == "preprint" and "arxiv.org" in why


def test_accepted_manuscript_phrase_beats_the_host():
    """An arXiv deposit of the accepted version is an accepted manuscript.

    Saying "preprint" there would *understate* the file, which is the opposite
    error and just as misleading in a citation.
    """
    text = "This is the author accepted manuscript. The final version is published in ..."
    v, why = provenance.classify(url="https://arxiv.org/pdf/2403.07618", text=text)
    assert v == "accepted"
    assert "accepted manuscript" in why.lower()


def test_publisher_host_is_asserted_not_verified():
    v, why = provenance.classify(url="https://onlinelibrary.wiley.com/doi/pdf/10.1/x")
    assert v == "vor"
    assert "asserted" in why


def test_unknown_host_without_markers_stays_unknown():
    v, why = provenance.classify(url="https://some-lab.example.edu/~a/paper.pdf", text="")
    assert v == "unknown"
    assert "not classified" in why


def test_no_url_no_text_is_unknown_not_a_guess():
    assert provenance.classify()[0] == "unknown"


def test_summarise_counts_every_version():
    recs = {"a": {"version": "preprint"}, "b": {"version": "vor"},
            "c": {"version": "vor"}, "d": {"version": "nonsense"}}
    counts = provenance.summarise(recs)
    assert counts == {"preprint": 1, "accepted": 0, "vor": 2, "unknown": 1}


def test_append_and_load_round_trip(tmp_path):
    rec = provenance.Provenance(file=str(tmp_path / "A_2020_X.pdf"),
                                version="preprint", version_basis="served by arxiv.org",
                                transport="plain", tier="L0")
    assert provenance.append(tmp_path, [rec]) == 1
    loaded = provenance.load(tmp_path)
    assert len(loaded) == 1
    only = list(loaded.values())[0]
    assert only["version"] == "preprint"
    assert only["version_basis"] == "served by arxiv.org"


def test_load_keeps_the_last_record_for_a_file(tmp_path):
    p = tmp_path / "A_2020_X.pdf"
    provenance.append(tmp_path, [provenance.Provenance(file=str(p), version="preprint")])
    provenance.append(tmp_path, [provenance.Provenance(file=str(p), version="vor")])
    loaded = provenance.load(tmp_path)
    assert loaded["A_2020_X.pdf"]["version"] == "vor"


def test_load_survives_a_corrupt_line(tmp_path):
    (tmp_path / provenance.PROVENANCE_FILE).write_text(
        '{"file": "a.pdf", "version": "vor"}\nnot json\n', encoding="utf-8")
    assert provenance.load(tmp_path)["a.pdf"]["version"] == "vor"


def test_same_file_name_in_two_layers_stays_two_entries(tmp_path):
    """A basename key silently merged two layers; a relative path does not."""
    (tmp_path / "L1").mkdir()
    (tmp_path / "L2").mkdir()
    provenance.append(tmp_path, [
        provenance.Provenance(file=str(tmp_path / "L1" / "Same_2020_X.pdf"), version="vor"),
        provenance.Provenance(file=str(tmp_path / "L2" / "Same_2020_X.pdf"), version="preprint"),
    ])
    loaded = provenance.load(tmp_path)
    assert len(loaded) == 2
    assert loaded["L1/Same_2020_X.pdf"]["version"] == "vor"
    assert loaded["L2/Same_2020_X.pdf"]["version"] == "preprint"


def test_a_relative_record_path_still_lands_on_the_same_key(tmp_path):
    provenance.append(tmp_path, [provenance.Provenance(file=str(tmp_path / "L1" / "A.pdf"),
                                                      version="preprint")])
    provenance.append(tmp_path, [provenance.Provenance(file="L1/A.pdf", version="vor")])
    assert len(provenance.load(tmp_path)) == 1
    assert provenance.load(tmp_path)["L1/A.pdf"]["version"] == "vor"


def test_a_file_name_token_is_accepted_but_labelled_weak():
    v, why = provenance.classify(filename="Wang_2026_World_Action_Models_arXiv.pdf")
    assert v == "preprint"
    assert "weak evidence" in why


def test_a_page_marker_outranks_the_file_name():
    text = "This is the author accepted manuscript. The final version is published in ..."
    v, _ = provenance.classify(text=text, filename="Thing_arXiv.pdf")
    assert v == "accepted"


def test_a_technical_report_is_a_preprint():
    v, why = provenance.classify(text="Technical Report No. 157, June 1988, UC Berkeley")
    assert v == "preprint"
    assert "technical report" in why.lower()
