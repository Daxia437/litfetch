"""The evidence record: one object per file, everything else a view over it.

This module is the integration point of the whole package. Identity, version,
provenance, reading status and (later) screening decisions describe the same
file, and a downstream tool should be able to read one JSON object rather than
call four APIs.

Two rules make the record worth having:

* **JSONL is the source of truth.** One line per version of the truth, appended,
  diffable, survives `git`. A database is a rebuildable index, never the record.
* **`schema` is versioned separately from the package.** A record written by 1.x
  must stay readable by 2.x; migrations are explicit functions, not silent
  rewrites.

The validator is deliberately hand-written and stdlib-only: a JSON Schema file
ships alongside for other languages, but a consumer that cannot install
`jsonschema` should still be able to check a record.
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional

SCHEMA_VERSION = 1

VERSIONS = ("preprint", "accepted", "vor", "unknown")
READING_STATUSES = ("A", "B", "C", "X")
IDENTITY_VERDICTS = ("match", "no_match", "unknown")
TIERS = ("L0", "L1", "LA", "L2", "L3", "manual", "")
SCREENING_DECISIONS = ("include", "exclude", "pending")

TOP_LEVEL = ("schema", "file", "sha256", "identity", "version",
             "provenance", "reading", "screening")

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def new_record(
    file: str,
    sha256: str = "",
    title_declared: str = "",
    title_printed: str = "",
    coverage: float = 0.0,
    verdict: str = "unknown",
    version_kind: str = "unknown",
    version_basis: str = "",
    url: str = "",
    tier: str = "",
    transport: str = "",
    retrieved: str = "",
    reading_status: Optional[str] = None,
    reading_evidence: str = "",
) -> Dict:
    """Build a record with every field present, so consumers never branch on absence."""
    rec: Dict = {
        "schema": SCHEMA_VERSION,
        "file": file,
        "sha256": sha256,
        "identity": {
            "title_declared": title_declared,
            "title_printed": title_printed,
            "coverage": round(float(coverage), 4),
            "verdict": verdict,
        },
        "version": {"kind": version_kind, "basis": version_basis},
        "provenance": {"url": url, "tier": tier, "transport": transport,
                       "retrieved": retrieved},
    }
    if reading_status is not None:
        rec["reading"] = {"status": reading_status, "evidence": reading_evidence}
    return rec


def validate(rec: Dict) -> List[str]:
    """Return a list of problems. Empty means valid.

    Strict about enums and types, lenient about extra keys inside the known
    sections: a future minor version may add a field there, and rejecting it
    would make forward compatibility impossible.
    """
    problems: List[str] = []
    if not isinstance(rec, dict):
        return ["record is not an object"]

    for key in ("schema", "file", "sha256", "identity", "version", "provenance"):
        if key not in rec:
            problems.append(f"missing required field: {key}")
    if problems:
        return problems

    if rec["schema"] != SCHEMA_VERSION:
        problems.append(f"schema is {rec['schema']!r}, expected {SCHEMA_VERSION}")

    if not isinstance(rec["file"], str) or not rec["file"].strip():
        problems.append("file must be a non-empty string")

    digest = rec["sha256"]
    if digest and not (isinstance(digest, str) and _HEX64.match(digest)):
        problems.append("sha256 must be 64 lowercase hex characters, or empty")

    identity = rec["identity"]
    if not isinstance(identity, dict):
        problems.append("identity must be an object")
    else:
        verdict = identity.get("verdict", "unknown")
        if verdict not in IDENTITY_VERDICTS:
            problems.append(f"identity.verdict {verdict!r} not in {IDENTITY_VERDICTS}")
        cov = identity.get("coverage", 0.0)
        if not isinstance(cov, (int, float)) or not 0.0 <= float(cov) <= 1.0:
            problems.append("identity.coverage must be a number in [0, 1]")

    version = rec["version"]
    if not isinstance(version, dict):
        problems.append("version must be an object")
    else:
        kind = version.get("kind", "unknown")
        if kind not in VERSIONS:
            problems.append(f"version.kind {kind!r} not in {VERSIONS}")
        if not version.get("basis"):
            # A label without its evidence is exactly what this project refuses.
            problems.append("version.basis is empty: a label must carry its reason")

    prov = rec["provenance"]
    if not isinstance(prov, dict):
        problems.append("provenance must be an object")
    else:
        tier = prov.get("tier", "")
        if tier not in TIERS:
            problems.append(f"provenance.tier {tier!r} not in {TIERS}")

    reading = rec.get("reading")
    if reading is not None:
        if not isinstance(reading, dict):
            problems.append("reading must be an object")
        elif reading.get("status") not in READING_STATUSES:
            problems.append(f"reading.status {reading.get('status')!r} "
                            f"not in {READING_STATUSES}")

    screening = rec.get("screening")
    if screening is not None:
        if not isinstance(screening, dict):
            problems.append("screening must be an object")
        elif screening.get("decision") not in SCREENING_DECISIONS:
            problems.append(f"screening.decision {screening.get('decision')!r} "
                            f"not in {SCREENING_DECISIONS}")

    unknown = [k for k in rec if k not in TOP_LEVEL]
    if unknown:
        problems.append(f"unknown top-level field(s): {', '.join(sorted(unknown))}")

    return problems


RECORD_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://example.invalid/litfetch/record-1.json",
    "title": "litfetch evidence record",
    "type": "object",
    "required": ["schema", "file", "sha256", "identity", "version", "provenance"],
    "additionalProperties": False,
    "properties": {
        "schema": {"const": SCHEMA_VERSION},
        "file": {"type": "string", "minLength": 1,
                 "description": "path relative to the library root"},
        "sha256": {"type": "string", "pattern": "^(|[0-9a-f]{64})$"},
        "identity": {
            "type": "object",
            "required": ["verdict"],
            "properties": {
                "title_declared": {"type": "string"},
                "title_printed": {"type": "string"},
                "coverage": {"type": "number", "minimum": 0, "maximum": 1},
                "verdict": {"enum": list(IDENTITY_VERDICTS)},
            },
        },
        "version": {
            "type": "object",
            "required": ["kind", "basis"],
            "properties": {
                "kind": {"enum": list(VERSIONS)},
                "basis": {"type": "string", "minLength": 1,
                          "description": "why this label; never a bare value"},
            },
        },
        "provenance": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "tier": {"enum": list(TIERS)},
                "transport": {"type": "string"},
                "retrieved": {"type": "string"},
            },
        },
        "reading": {
            "type": "object",
            "properties": {
                "status": {"enum": list(READING_STATUSES)},
                "evidence": {"type": "string"},
            },
        },
        "screening": {
            "type": "object",
            "properties": {
                "decision": {"enum": list(SCREENING_DECISIONS)},
                "reason": {"type": "string"},
                "by": {"type": "string"},
                "at": {"type": "string"},
            },
        },
    },
}


def schema_json(indent: int = 2) -> str:
    return json.dumps(RECORD_SCHEMA, indent=indent, ensure_ascii=False) + "\n"


def from_provenance(rec, reading_status: Optional[str] = None,
                    reading_evidence: str = "") -> Dict:
    """Adapt a `Provenance` dataclass (the file-level record) to a record.

    Kept here rather than in `provenance.py` so that the schema module owns the
    mapping in both directions: when the schema changes, this is the one place
    that has to follow.
    """
    kind = getattr(rec, "version", "unknown")
    return new_record(
        file=getattr(rec, "file", ""),
        sha256=getattr(rec, "sha256", ""),
        title_declared=getattr(rec, "title_declared", ""),
        title_printed=getattr(rec, "title_printed", ""),
        coverage=getattr(rec, "coverage", 0.0),
        verdict="match" if kind != "unknown" else "unknown",
        version_kind=kind,
        version_basis=getattr(rec, "version_basis", ""),
        url=getattr(rec, "url", ""),
        tier=getattr(rec, "tier", ""),
        transport=getattr(rec, "transport", ""),
        retrieved=getattr(rec, "retrieved", ""),
        reading_status=reading_status,
        reading_evidence=reading_evidence,
    )
