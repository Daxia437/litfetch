# Releasing

Two artefacts come out of this repository: a **source tree on GitHub** and, if the
name is free, a **package on PyPI**. GitHub needs no permission from anyone; PyPI
needs the name.

## 0. The name is the gate

`litfetch` on PyPI is owned by another project (populationgenomics, since
2026-07-03) with an adjacent purpose. Nothing here can be uploaded as `litfetch`.
Decide first:

| option | what it means | cost |
|---|---|---|
| **rename** | change `[project] name`, the `litfetch/` package directory, the console script, `~/.workbuddy/skills/lit-fetch/`, and the skill's own name | ~1 h, mechanical |
| **GitHub only** | keep the name, install with `pip install "pkg[all] @ git+https://github.com/…"`, say in the README that the PyPI name belongs to someone else | 0 |

Names checked free on PyPI on 2026-11-05: `litcheck`, `litverify`, `paperproof`,
`citationproof`, `pdfattest`, `veripaper`, `citationaudit`, `litattest`,
`paperaudit`. (`citecheck` is taken.) Re-check before committing to one.

The repository already exists at `github.com/Daxia437/litfetch` and `pyproject.toml`
points at it. If the project is renamed, move the repository too (`gh repo rename`)
and update those URLs in the same commit -- metadata pointing nowhere is worse than
no metadata.

## 1. First commit

`git` is installed (2.54.0, Apple Command Line Tools). The `/usr/bin/git` shim
refuses to run until the **Xcode** licence is accepted — the developer directory
points at `/Applications/Xcode.app` — so `~/.local/bin/git` execs
`/Library/Developer/CommandLineTools/usr/bin/git` directly, and `~/.local/bin` is
prepended to `PATH` in `~/.zshrc`. Other tools and IDEs still call `/usr/bin/git`;
the durable fix for those is `sudo xcodebuild -license accept` in a Terminal.

`gh` 2.103.3 is installed at `~/.local/opt/gh-2.103.3` (symlinked into
`~/.local/bin`). Authentication is per-user and is **not** configured here:

```bash
gh auth login          # GitHub.com → HTTPS → login with a browser
gh auth status         # should report one account
```

Two things must exist before the first commit, or it fails with "Please tell me
who you are":

```bash
git config --global user.name  "Your Name"
git config --global user.email "you@example.org"
```

Then:

```bash
cd tools/litfetch
git init
git branch -M main
git add -A
git commit -m "litfetch 1.4.0: verify a PDF against the citation, and label its version"
```

`.gitignore` already excludes build output, `*.egg-info/`, `__pycache__/`,
`.DS_Store` and `_libraries/`. Before the first commit, confirm no local library
is staged:

```bash
git status --short | grep -i -E '\.pdf$|_libraries/|PROVENANCE' || echo "clean: no data files staged"
```

Data never belongs in the repository: the PDFs in `_libraries/` are third-party
copyrighted files, and the licence covers the tool, not the corpus.

## 2. Publish the source

Create an empty repository on GitHub (no README, no licence — both exist here),
then:

```bash
git remote add origin git@github.com:<org>/<name>.git
git push -u origin main
```

`ci.yml` runs on the push: 3 operating systems × Python 3.10/3.12/3.13, a job that
installs **no extras** (the standard-library contract for `core`), and a job that
builds the sdist and wheel, installs the wheel into a clean venv, and checks the
bundled skill. Python 3.9 was dropped rather than claimed: it is end-of-life and the
run had become unverifiable (setup-python 504s fetching 3.9 on two runner images).

## 3. Tag a release

```bash
git tag -s v1.4.0 -m "1.4.0"
git push origin v1.4.0
```

Then draft a GitHub Release from the tag and attach `dist/*.whl`,
`dist/*.tar.gz` and their `*.sha256`. This works today, with or without PyPI.

## 4. PyPI, only after the name is settled

Use **trusted publishing** (OIDC) — no API token in repository secrets.

1. On PyPI: *Your projects → Publishing → Add a pending publisher*; fill in the
   repository, workflow `release.yml`, environment `pypi`.
2. On GitHub: *Settings → Environments → New environment* named `pypi`
   (optionally require a reviewer).
3. Add `.github/workflows/release.yml`:

```yaml
name: release
on:
  push:
    tags: ["v*"]
permissions:
  contents: read
jobs:
  publish:
    runs-on: ubuntu-latest
    environment: pypi
    permissions:
      id-token: write          # trusted publishing
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: python -m pip install build && python -m build
      - uses: pypa/gh-action-pypi-publish@release/v1
```

The first upload creates the project; later ones need no new configuration.

## 5. What not to promise

* **PyMuPDF is AGPL-3.0** and is an optional extra. The wheel does not contain it,
  so this project stays MIT. A bundle that *does* contain it (frozen app,
  container image) inherits AGPL obligations — see `NOTICE`.
* The core is stdlib-only by design. Adding a hard dependency is a breaking
  change to that promise, not an implementation detail.
* Version policy in `INTEGRATION_PLAN.md` §11 says `0.x` until the record schema
  has survived three months of use; the package is currently `1.4.0`. Pick one
  and make the README, the tag and the plan agree.
