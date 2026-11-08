"""Shared test helpers.

`Skip` lives here rather than in run_tests.py because the runner is executed as
`__main__`: a test importing it from `run_tests` would get a second copy of the
class, and `except Skip` in the runner would not catch it. One module, one class.
"""


class Skip(Exception):
    """A test that cannot run here -- usually a missing optional extra.

    A whole module is skipped by ImportError; this is the per-test version, so a
    suite running without `pymupdf` says *why* one check did not happen.
    """
