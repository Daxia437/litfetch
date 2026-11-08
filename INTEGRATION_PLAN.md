# From `litfetch` to an evidence-integrity pipeline — feasibility and integration plan

**Status**: proposal, 2026-10-22. Not a commitment.
**Scope of the question**: merge the `lit-fetch` skill with the `litfetch` package, absorb the useful parts of existing open-source
(bio)medical literature tooling, and ship "the single most capable product on the market".

**Reading note.** The request says "开源医药项目". Two readings are possible: (a) *biomedical* literature tooling — PubMed,
Europe PMC, systematic-review automation; (b) *existing* open-source projects in general. This plan assumes (a), because that is
the reading that changes the design; every adapter in §4 is optional, so under reading (b) the same architecture stands with
the biomedical ring empty.

---

## 1. Verdict, first

| question | answer |
|---|---|
| Can the skill and the package be merged? | **Yes — done 2026-10-22, release 1.3.0.** See §5.1 and §12. |
| Can we integrate the useful parts of the existing OSS? | **Yes, through adapters**, with two hard constraints: no vendoring of copyleft, and no runtime dependency added to the core. §4, §10 |
| Can we ship "the most capable single product on the market"? | **No — not as stated, and the attempt would lose.** §1.1 |
| Is there a defensible product here? | **Yes, but it is a kernel, not a platform.** §1.2 |
| Should this project build it now? | **Not the full product.** §14 gives the decision rule and the minimum public surface. |

### 1.1 Why "most capable" is the wrong target

The three axes on which such a product is usually judged are all lost before we start:

* **Coverage.** Reference managers already do "find me the PDF": Zotero with open-access plugins, Paperpile, EndNote. They
  have ten years of publisher relationships and a browser extension that solves the bot wall *by being a browser*. We will not
  out-cover them.
* **Screening throughput.** Rayyan, ASReview, Covidence and friends are built around active-learning screening at thousands of
  records. Re-implementing that is a research programme, not an integration.
* **Comprehension.** Elicit, Consensus, SciSpace and every LLM reading tool answer "what does this paper say". We would be
  competing with a feature that improves monthly and costs nothing to add.

Chasing all three produces the fourth-best version of each. The interesting question is not "what else can it do" but
"what can it *prove* that the others cannot".

### 1.2 The defensible position

Everything in this package exists because of one failure class: **a file that passes for a paper and is not the paper you
think it is** — the wrong paper, a landing page, a sequel, the preprint of the article you cite, the 1988 technical report of
the 1991 paper. The 2026-10 audit produced seven instances in one library.

No mainstream tool treats that as a first-class object. Zotero stores whatever the browser saved; Unpaywall answers "is there
an open copy"; neither answers **"is this file the paper, and which version of it is it?"** — with evidence, per file, in a
form a reviewer can audit.

So the product is:

> **A verification kernel for the literature you actually hold.** It proves identity, labels version, records provenance, and
> emits an auditable ledger — then gets out of the way and lets Zotero, PubMed, GROBID and your agent do what they do.

That is a smaller claim than "most powerful". It is also the only one that survives contact with the market, because it is
**complementary**: it can be adopted by the tools that already won the other three axes.

---

## 2. What we actually have

Measured on this repository today, not estimated:

| asset | evidence |
|---|---|
| 11 modules, 3 260 lines, standard library only | `tools/litfetch/litfetch/` |
| 74 passing tests (963 lines), dependency-free runner | `python tests/run_tests.py` → 74 passed |
| Four identity conditions with an adjacency rule | title must sit in the **largest-type block of page 1**, as a contiguous run |
| Version labels with evidence | `PROVENANCE.jsonl`; 160 files → `vor 0 · accepted 3 · preprint 59 · unknown 98` |
| Failure taxonomy | `cloudflare / botwall / challenge / paywall / js_required / dead / not_pdf` |
| Reading ledger, regenerable | `READING_STATUS.md`, marker-delimited |
| Adapter discipline already in place | optional extras (`pymupdf`, `curl_cffi`, `websocket-client`), each degrading visibly |

Two properties are worth naming because they are expensive to retrofit and we already have them:

1. **Verification is independent of the source.** Any byte stream can be admitted from anywhere, because the check is on the
   bytes. This is what makes a "wild web" tier safe, and it is what makes every integration in §4 cheap.
2. **The ledger is derived, never authoritative.** Status is recomputed from a bibliography plus an override table, so it can
   be regenerated and diffed. Most tools store status as editable state and cannot tell you why.

---

## 3. Integration scope

Three rings. The boundary between ring 2 and ring 3 is the whole design.

### Ring 1 — in the core, no optional dependency

Identifiers and metadata (Crossref, OpenAlex, Unpaywall, Semantic Scholar, arXiv), PDF integrity, provenance and version,
reading ledger, failure diagnosis, quarantine and audit trail. This is what `litfetch` already is; the merge with the skill
makes it one installable artefact.

### Ring 2 — adapters, optional, never required

| capability | source | why it belongs |
|---|---|---|
| Biomedical retrieval | **PubMed E-utilities**, **Europe PMC** REST | the 医药 half: a query becomes a reference list we can verify; PMC gives full text XML, which removes the PDF problem entirely for OA articles |
| Structured extraction | **GROBID** (Apache-2.0, REST, Docker) | replaces our font-size heuristic with a trained header/reference parser; also yields the paper's **reference list**, which enables citation-level verification (§5.3) |
| Library interchange | **Zotero** export (CSL-JSON / BibTeX) | turns "I already have 800 PDFs" into a verification queue; the single best adoption path |
| Screening bookkeeping | PRISMA counts derived from our own ledger | reviewers need the flow diagram; we already know identified/screened/included because every decision is a row |
| Fuzzy matching accelerator | RapidFuzz (MIT) | optional speed-up for title scoring; the stdlib implementation stays as fallback |

### Ring 3 — explicitly out

* **Sci-Hub or any paywall circumvention.** Non-negotiable; it would also destroy the one thing we sell.
* **A reference-manager UI.** Zotero exists and is better at it.
* **Active-learning screening.** ASReview exists and is better at it.
* **LLM summarisation / "chat with the paper".** Improves monthly elsewhere; not our moat.
* **Citation-graph analytics.** ResearchRabbit / OpenAlex do it.

Stating ring 3 in the README is a feature: it tells a reader we are not going to abandon them in six months for a pivot.

---

## 4. Core functions to integrate

Each row is an **adapter contract**, not a dependency: a module exposing `available() -> bool` and one or two pure functions.
Absent the extra or the service, the adapter reports itself unavailable and the pipeline degrades as it already does today.

### 4.1 PubMed / Europe PMC (the 医药 ring)

```
search(query, limit) -> [Reference]          # esearch + esummary, or Europe PMC /search
fulltext(pmcid) -> bytes | tei               # Europe PMC /fullTextXML
```

* Rate limits are explicit and honoured: E-utilities allow 3 requests/second without a key, 10 with one; Europe PMC asks for
  a contact address. `sources.py` already has per-host pacing and `Retry-After`; this reuses it.
* **Why it matters beyond coverage**: for PMC open-access articles we can fetch **JATS XML instead of a PDF**. That sidesteps
  the entire bot-wall problem for that subset, and gives a text layer that is exact rather than OCR'd.
* Provenance: an XML full text is labelled `vor (PMC OA)` or `accepted (author manuscript)`; PMC distinguishes them in the
  deposit metadata, and that distinction is exactly what our `version` field exists to carry.

### 4.2 GROBID

```
header(pdf) -> {title, authors, doi, year, venue}      # /api/processHeaderDocument
references(pdf) -> [{title, authors, doi, year}]       # /api/processReferences
tei(pdf) -> str                                        # /api/processFulltextDocument
```

* Apache-2.0, Java, REST, Docker; ~3 PDF/s server-side. **Called over HTTP, never vendored**, so no licence obligation
  propagates into our tree.
* Two uses. First, it makes verification robust on ugly layouts where "largest type on page 1" is wrong. Second, and more
  interesting, `references()` gives us the bibliography **as data** — which turns verification around: instead of checking a
  file against a title we already have, we can check *a paper's own claims about what it cites*.
* It does not run on Windows natively. Since it is optional and remote, that is acceptable; the documented answer is
  "run the container, or skip the adapter".
* Degradation: without GROBID, the current heuristic stays. This is one of the few places where the fallback is genuinely
  good enough, which is why GROBID can stay optional.

### 4.3 Zotero interchange

```
read_export(csl_json | bibtex) -> [Reference]          # no Zotero API, no plugin, no AGPL surface
```

* **Zotero is AGPL-3.0.** We do not link it, import it, or copy from it. We read a file the user exported. That is data, not a
  derivative work, and the README says so explicitly so a corporate user's legal team can sign off in one minute.
* The killer workflow is one command: `litfetch audit --from-zotero library.json --report audit.md`. It answers, for an
  existing library, "how many of these 800 files are the paper they claim to be, and which are preprints" — a question nobody
  can currently answer without opening all 800.

### 4.4 PRISMA bookkeeping

Not an integration but a view over data we already hold: identified / duplicates removed / screened / excluded (with reason) /
included. Every row in the ledger is a decision with a timestamp and an evidence string, so the flow diagram is a `GROUP BY`,
not a separate database.

### 4.5 What we deliberately borrow as *ideas*, not code

From `fulltext-article-downloader`: per-route provenance. From `scihub-cli`: fail-fast on a challenge page, `Retry-After`.
From `DoiHunter`: scored title resolution with a rejection threshold. From the Zotero reading-status plugins: status as a
recorded attribute rather than a recomputed guess. All four are already implemented; the table in `README.md` credits them.

---

## 5. How the modules compose

### 5.1 Merge the skill into the package

Today the skill is a shell around the CLI and the CLI is a shell around six modules. Three layers where one would do. The
merge:

```
litfetch/                 # one installable distribution
  core/                   # no network, no optional deps
    verify.py             # four conditions
    provenance.py         # version + evidence
    store.py              # naming, hashing, quarantine
    ledger.py             # A/B/C/X
    model.py              # EvidenceRecord  (new)
  io/                     # network adapters, each optional
    crossref.py openalex.py unpaywall.py semanticscholar.py arxiv.py
    pubmed.py europepmc.py grobid.py zotero.py
    fetch.py browser.py diagnose.py
  cli/
  skill/SKILL.md          # ships inside the wheel; `litfetch skill install` writes it
```

The skill stops being a separate thing to maintain: `litfetch skill install --target claude|codex|copilot` writes the same
`SKILL.md` we test in CI. One version number for both, which removes the drift we have already had once (skill 3.0 vs package
1.2.1).

### 5.2 The data model is the integration point

One record per file, one schema, everything else a view over it:

```jsonc
{
  "schema": 1,
  "file": "L14/Fan_Jianqing_1988_....pdf",
  "sha256": "…",
  "identity": { "title_printed": "…", "title_declared": "…",
                "coverage": 0.83, "adjacent_run": "6/6", "verdict": "match" },
  "version": { "kind": "preprint", "basis": "page 1 says \"Technical Report\"" },
  "provenance": { "url": "…", "tier": "LA", "transport": "plain",
                  "retrieved": "2026-10-22T03:01:11Z", "route": "author-archive" },
  "reading": { "status": "A", "evidence": "R143" },
  "screening": { "decision": "include", "reason": "…", "by": "…", "at": "…" }
}
```

* **JSONL is the source of truth** (append-only, diffable, survives `git`); **SQLite is a rebuildable index** for queries over
  thousands of rows. Never the other way round — the moment the database is authoritative, the audit trail is gone.
* `schema` is versioned separately from the package. A record written by 1.x must be readable by 2.x; migrations are explicit
  functions, not silent rewrites.
* This one decision is what lets Zotero, PubMed, GROBID and the CLI all be adapters rather than features.

### 5.3 The composition that only exists if all the parts are present

```
PubMed query ──► Reference[] ──► resolve ──► fetch ──► verify ──► provenance ──► ledger ──► PRISMA
                                    │                    │
                                    └── GROBID ──────────┘
                                        (header, when the heuristic fails)
                                             │
                                        references() ──► citation check ──► "this paper cites X, but the X in our
                                                                            library is a different X"
```

The last node is the one nobody else can build: **a citation-level consistency check across the corpus you hold**, made
possible only because every file carries a verified title and a version. It is a research-relevant capability, not a
feature-list item — it is the difference between "I have 800 PDFs" and "I know which of my 800 PDFs disagree with what my
papers claim about them".

---

## 6. Differentiation, stated against real alternatives

| | Zotero + plugins | Rayyan / ASReview | Elicit / Consensus | downloaders (`fulltext-…`, `DoiHunter`) | **this** |
|---|---|---|---|---|---|
| stores references | ●●● | ●● | ○ | ○ | ○ (by design) |
| finds a PDF | ●●● | ●● | ●● | ●●● | ●● |
| proves the PDF is the paper | ○ | ○ | ○ | ● | ●●● |
| says *which version* | ○ | ○ | ○ | ○ | ●●● |
| explains *why* a download failed | ○ | ○ | ○ | ● | ●●● |
| auditable reading status | ● (plugins) | n/a | n/a | ○ | ●●● |
| screening at scale | ○ | ●●● | ●● | ○ | ○ (exports to them) |
| summarises content | ○ | ○ | ●●● | ○ | ○ |

Read the table as a division of labour, not a scoreboard. Two rows are ours alone and both are the same underlying object:
**the file's identity is a claim, and claims need evidence**.

The moat is not the code — 3 260 lines can be copied. It is the accumulated set of failure modes, each with a regression test:
the sequel that scores 1.00 on a word bag, the landing page that passes a magic-byte check, the same author's other paper,
the technical report with the journal article's title. A competitor starts at zero on that list. The list is in `docs/failure-modes.md`; it only grows when something is caught in the wild.

---

## 7. Public surface, and what 1.0 would promise

1. **CLI** — stable; every command has `--json`; exit codes 0/1/2 documented (already true).
2. **Python API** — `from litfetch import verify_pdf, inspect_pdf, Library, classify`. Small, typed, no globals.
3. **Skill** — one file, generated from the CLI's own `--help` plus prose, so it cannot drift.
4. **Record schema** — versioned, documented, with a JSON Schema file so other tools can consume it.

What 1.0 promises: *the four conditions and the record schema will not break without a major version.* Nothing about network
sources — those rot, and promising them would be a lie.

---

## 8. Repository structure

```
litfetch/
├── README.md                     # what it proves, with the measured numbers
├── LICENSE                       # MIT (code)
├── LICENSE-docs                  # CC-BY-4.0 (prose, figures)
├── NOTICE                        # third-party ideas and their licences
├── CONTRIBUTING.md
├── CODE_OF_CONDUCT.md            # Contributor Covenant
├── SECURITY.md                   # threat model: untrusted PDFs, network egress
├── CHANGELOG.md                  # Keep a Changelog, generated from tags
├── CITATION.cff                  # so the tool itself is citable
├── pyproject.toml
├── docs/
│   ├── architecture.md           # core / adapters / data model
│   ├── schema/record.schema.json
│   ├── adapters/                 # one page per adapter: contract, limits, licence
│   ├── adr/                      # 0001-core-vs-adapter.md, 0002-jsonl-vs-sqlite.md, …
│   ├── failure-modes.md          # every known way a file lies, each with its test
│   └── provenance.md             # what a version label does and does not mean
├── litfetch/
│   ├── core/                     # verify · provenance · store · ledger · model
│   ├── io/                       # sources · fetch · diagnose · browser
│   ├── cli.py
│   └── skill/SKILL.md            # shipped in the wheel (package data)
├── tests/
│   ├── unit/                     # the 74, rehomed
│   ├── fixtures/                 # small real PDFs: a sequel, a landing page, an offprint
│   └── e2e/                      # skipped unless the network is available
├── examples/
│   ├── refs.json
│   └── zotero-audit/             # input export + expected report
└── .github/
    ├── workflows/ci.yml          # matrix + lint + types + schema check
    ├── workflows/release.yml     # tag → build → PyPI → GitHub Release
    ├── ISSUE_TEMPLATE/{bug,adapter,false_positive}.yml
    └── PULL_REQUEST_TEMPLATE.md
```

**Why `docs/failure-modes.md` is a first-class document**: it is the product's memory. Each entry is a real misfiled paper,
the check that catches it, and the test that pins it. It doubles as the contribution on-ramp — "add failure mode fifteen".

---

## 9. Documentation

| file | audience | must contain |
|---|---|---|
| README | evaluator, 90 seconds | the claim, one command, one measured number, the limits |
| `docs/architecture.md` | contributor | core/adapter boundary, why the core has no network |
| `docs/failure-modes.md` | contributor, sceptic | every known way a file lies, the check that catches it, the test that pins it |
| `docs/adapters/*.md` | integrator | contract, rate limits, licence, degradation |
| `docs/schema/record.schema.json` | downstream tool | the record, formally |
| `CONTRIBUTING.md` | newcomer | "add an adapter in 80 lines", with a template |
| `SECURITY.md` | institutional user | PDFs are untrusted input; no upload; egress list |

---

## 10. Licensing and third-party compliance

| component | licence | how we use it | obligation |
|---|---|---|---|
| our code | **MIT** | — | attribution only |
| our prose and figures | **CC-BY-4.0** | — | attribution |
| GROBID | Apache-2.0 | HTTP call to a service the user runs | attribution in `NOTICE`; **do not vendor** |
| Zotero | **AGPL-3.0** | read an exported CSL-JSON/BibTeX file | none — data, not code; state this in `NOTICE` |
| PubMed E-utilities / Europe PMC | API terms | HTTP | contact address, rate limits, attribution |
| Crossref / OpenAlex / Unpaywall | metadata licences (CC0 / CC-BY family — verify per service) | HTTP | attribution; do not redistribute bulk data |
| RapidFuzz | MIT | optional extra | attribution |

Rules the project should adopt now, because retrofitting licence hygiene is expensive:

1. **No vendoring of third-party code.** Adapters call services; they do not embed them.
2. **No AGPL anywhere in the dependency graph.** Zotero stays a file format, never a link.
3. `NOTICE` names every borrowed *idea*, with the project it came from. We already do this in the README; it belongs in a
   legal file too.
4. A CI job fails the build if a new dependency appears without a line in `NOTICE`.

---

## 11. Versioning and release

* **SemVer**, with a hard rule: the **record schema** has its own version and a breaking schema change is a major bump even if
  the code is unchanged.
* `0.x` until the schema has survived three months of real use; `1.0` when §7's promise can be kept.
* Deprecation: one minor version of warning, printed at runtime with the replacement named.
* Tags `vX.Y.Z`; signed. `CHANGELOG.md` generated from conventional commits.

Release pipeline (GitHub Actions):

```
tag v*.*.*
  ├─ CI matrix: {ubuntu, macos, windows} × {3.10, 3.12, 3.13} + no-extras job (core must stay stdlib)
  ├─ build sdist + wheel, verify install in a clean venv, run `litfetch doctor`
  ├─ PyPI trusted publishing (OIDC, no token in secrets)
  ├─ SBOM (CycloneDX) + `pip-audit`
  ├─ GitHub Release: notes from CHANGELOG + the wheel + SBOM
  └─ smoke: `pipx install litfetch && litfetch verify examples/…`
```

The **no-extras job is the important one**: if the core ever grows a hard dependency, CI says so on the same commit.

---

## 12. Phased plan, with exit criteria

| phase | deliverable | exit criterion (falsifiable) |
|---|---|---|
| **P0** (merge) | skill folded into the wheel; `core/`-`io/` split; `record.schema.json` | 74 tests still pass; `pip install .` then `litfetch skill install` yields a byte-identical SKILL.md |
| **P1** (Zotero audit) | `litfetch audit --from-zotero` | on a 200-item export: ≥95% verified, **≤2% false accepts** (hand-checked), report generated in <5 min |
| **P2** (biomedical) | PubMed + Europe PMC adapters; JATS path | a 50-record query completes under the published rate limits; PMC XML path stores no PDF and still gets a version label |
| **P3** (GROBID) | optional adapter; header used when the heuristic is not exact | on 30 hard PDFs where the heuristic fails, GROBID recovers ≥80% of titles, and no case regresses from verified to wrong |
| **P4** (PRISMA) | screening decisions + flow export | a full flow diagram regenerated from records alone, diffable against the previous run |

Each phase is independently useful. None requires the next.

### P0 status: done (2026-10-22, v1.3.0)

| exit criterion | result |
|---|---|
| the 74 tests still pass | **94 pass** (74 kept, 20 added for the split, the record and the skill) |
| `pip install .` then `litfetch skill install` yields a byte-identical SKILL.md | wheel built, installed into a clean target, `skill` then `skill --check` → `identical`, `cmp` against the repo copy → equal |

What changed, in the order it mattered:

1. `litfetch/{core,io}/` split; `cli.py` stays a single module (a `cli/` package would add a file, not a
   boundary). Imports rewritten; the public names are lazy re-exports (PEP 562) so that
   `import litfetch.core.store` does not drag in the network half.
2. `core/model.py` — the evidence record plus a hand-written validator and a JSON Schema that are checked
   against each other by a test (`schema` command prints the schema; enums cannot drift).
3. The skill moved into `litfetch/skill/SKILL.md` as package data, with `litfetch skill [--check|--print]`.
   The installed copy at `~/.workbuddy/skills/lit-fetch/SKILL.md` is now written by the tool, not by hand.
4. **One version number.** The first wheel built reported `1.3.0` from `pyproject.toml` while
   `litfetch --version` said `1.2.1` — the exact drift P0 exists to remove. The version now lives only in
   `litfetch/__init__.py` and `pyproject.toml` reads it with `dynamic = ["version"]`.

The one thing P0 did **not** do: `model.py` is not yet written by `provenance.py` — the storage layer still
emits `Provenance` records to JSONL. The adapter `model.from_provenance()` exists and is tested; switching
the writer over is a schema migration, and it belongs with P1 rather than with a packaging release.

---

## 13. Risks, and what would make me stop

| risk | mitigation | kill signal |
|---|---|---|
| adapter rot (APIs change) | adapters optional, failures loud, contract tests weekly | >2 adapters broken for >1 month → cut them |
| scope creep into a reference manager | ring 3 written into the README | a PR adding a UI |
| licence contamination | no vendoring, no AGPL, CI check | any AGPL in `pip graph` |
| "most powerful" claim backfires | never ship the claim; ship the measurement | a benchmark we cannot reproduce |
| maintenance cost falls on one researcher | adapters as the contribution surface; "good first issue" per adapter | no external contributor after 6 months → keep it private |
| the verifier is wrong in a new way | every failure becomes a fixture + test | a false accept found in the wild → stop adding features until it is a test |

---

## 14. Recommendation

**Do not build the platform.** Build the kernel and publish it whole:

1. **Merge the skill into the package now** (P0). It removes a real drift risk and costs a day.
2. **Publish as a small tool with a strong claim**, MIT, one command, the failure modes documented. The audience is not
   "everyone who reads papers" — it is the person who has to defend a citation.
3. **Ship adapters, not integrations.** Zotero export first (highest adoption per line of code), PubMed/Europe PMC second (the
   医药 half), GROBID third (the quality half). Each is a page of docs and ~80 lines.
4. **Refuse the three races** in §1.1 in writing, in the README, so the product does not drift into being a worse Zotero.
5. **Keep the research project separate.** This repository's job is a mathematical claim about identifiability, and the tool
   exists because that claim needs an evidence trail. If the tool starts consuming research time, the correct move is to
   freeze it at P1 and cite it, not to grow it.

The strongest version of this product is not the one with the most features. It is the one that a reviewer, a co-author or a
journal's data editor can run against a bibliography and get an answer they are willing to sign their name under.
