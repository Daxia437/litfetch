"""litfetch — prove that a PDF is the paper it claims to be, and say which version it is.

The package is split in two, and the split is the contract:

    litfetch.core    pure: no network, no optional dependency.
                     Identity, version, storage, reading ledger, record schema.
    litfetch.io      everything that touches the world: Crossref, OpenAlex,
                     Unpaywall, arXiv, Semantic Scholar, HTTP, Chrome.
    litfetch.cli     a thin shell over both.

`core` must stay importable on a machine with no network, no `pymupdf` and no
`curl_cffi`; the test suite checks that statically and CI repeats it with a job
that installs no extras. Anything that reaches outside belongs in `io`.

The re-exports below are **lazy** (PEP 562) on purpose: `import litfetch.core.store`
must not drag in the network half just because the package `__init__` was executed.

Public surface (stable within a major version):

    verify_pdf(path, expected_title=..., expected_year=...)  -> Verdict
    inspect_pdf(path, url=...)                               -> Provenance
    Library(root)                                            -> storage
    classify_response(status, content_type, head, size)      -> (verdict, detail)
    classify_reachability(num, title, urls, doi="")           -> reachability.Verdict
    classify_url(url)                                        -> reachability.Verdict
    title_score(expected, candidate)                         -> float
"""

from __future__ import annotations

import importlib

__version__ = "1.1.1"

_EXPORTS = {
    # identity
    "verify_pdf": ("core.verify", "verify_pdf"),
    "Verdict": ("core.verify", "Verdict"),
    "sniffs": ("core.verify", "sniffs"),
    "sha256_of": ("core.verify", "sha256_of"),
    # version and provenance
    "inspect_pdf": ("core.provenance", "inspect_pdf"),
    "Provenance": ("core.provenance", "Provenance"),
    "classify_version": ("core.provenance", "classify"),
    # storage and ledger
    "Library": ("core.store", "Library"),
    "classify_reading": ("core.ledger", "classify"),
    "parse_bibliography": ("core.ledger", "parse_bibliography"),
    # the outside world
    "classify_response": ("io.diagnose", "classify"),
    "classify_reachability": ("io.reachability", "classify"),
    "classify_url": ("io.reachability", "url_verdict"),
    "probe": ("io.diagnose", "probe"),
    "Reference": ("io.sources", "Reference"),
    "resolve": ("io.sources", "resolve"),
    "title_score": ("io.sources", "title_score"),
}

__all__ = ["__version__", *_EXPORTS]


def __getattr__(name):
    try:
        module_path, attr = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(importlib.import_module(f".{module_path}", __name__), attr)
    globals()[name] = value          # cache: the second lookup is a plain attribute
    return value


def __dir__():
    return sorted({*globals(), *__all__})
