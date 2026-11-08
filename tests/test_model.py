"""The evidence record: one object per file, and a label must carry its reason."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from litfetch.core import model  # noqa: E402


def _rec(**kw):
    base = dict(file="L14/A_2020_X.pdf", sha256="a" * 64,
                title_declared="T", title_printed="T", coverage=0.9,
                verdict="match", version_kind="preprint",
                version_basis="served by arxiv.org", url="https://arxiv.org/pdf/1",
                tier="L0", transport="plain", retrieved="2026-10-22T03:00:00Z")
    base.update(kw)
    return model.new_record(**base)


def test_a_complete_record_is_valid():
    assert model.validate(_rec()) == []


def test_a_version_label_without_a_reason_is_invalid():
    """The project's whole discipline in one rule: never a bare label."""
    problems = model.validate(_rec(version_basis=""))
    assert any("basis" in p for p in problems)


def test_unknown_version_kind_is_rejected():
    problems = model.validate(_rec(version_kind="published-ish"))
    assert any("version.kind" in p for p in problems)


def test_coverage_outside_the_unit_interval_is_rejected():
    assert any("coverage" in p for p in model.validate(_rec(coverage=1.5)))


def test_a_bad_hash_is_rejected_but_an_empty_one_is_allowed():
    assert any("sha256" in p for p in model.validate(_rec(sha256="deadbeef")))
    assert model.validate(_rec(sha256="")) == []


def test_an_unknown_top_level_field_is_rejected():
    """Strict at the top level so that a typo cannot silently become a new field."""
    rec = _rec()
    rec["verison"] = {"kind": "vor"}
    assert any("unknown top-level" in p for p in model.validate(rec))


def test_extra_fields_inside_a_section_are_tolerated():
    """Forward compatibility: a later minor version may add a field there."""
    rec = _rec()
    rec["provenance"]["route"] = "author-archive"
    assert model.validate(rec) == []


def test_identity_verdict_must_be_from_the_enum():
    assert any("identity.verdict" in p for p in model.validate(_rec(verdict="probably")))


def test_screening_decision_is_validated_when_present():
    rec = _rec()
    rec["screening"] = {"decision": "maybe"}
    assert any("screening.decision" in p for p in model.validate(rec))
    rec["screening"] = {"decision": "include", "reason": "matches PICO"}
    assert model.validate(rec) == []


def test_the_json_schema_matches_the_validator_enums():
    schema = json.loads(model.schema_json())
    props = schema["properties"]
    assert props["version"]["properties"]["kind"]["enum"] == list(model.VERSIONS)
    assert props["reading"]["properties"]["status"]["enum"] == list(model.READING_STATUSES)
    assert schema["required"] == ["schema", "file", "sha256", "identity", "version", "provenance"]
    assert props["schema"]["const"] == model.SCHEMA_VERSION


def test_from_provenance_carries_the_label_across():
    from litfetch.core.provenance import Provenance

    prov = Provenance(file="L14/A.pdf", sha256="b" * 64, version="preprint",
                      version_basis="page 1 says \"arXiv:1234.5678\"",
                      url="https://arxiv.org/pdf/1234.5678", tier="L0")
    rec = model.from_provenance(prov, reading_status="A", reading_evidence="R141")
    assert model.validate(rec) == []
    assert rec["version"]["kind"] == "preprint"
    assert rec["reading"]["status"] == "A"
