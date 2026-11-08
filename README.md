# litfetch

Fetch academic PDFs in batches, and **prove that each file is the paper it claims to be**.

Built after a 2026-10 audit in which 10 of ~100 downloaded papers turned out to be the wrong paper,
7 "PDFs" turned out to be HTML landing pages, and none of it was noticed until someone tried to cite them.
The download was never the hard part. The verification was.

Pure standard library. No network SDK, no browser automation required, no config file. Runs the same on
macOS, Linux and Windows with Python 3.10+.

---

## Install

```bash
# isolated, recommended
pipx install "litfetch[all] @ git+https://github.com/Daxia437/litfetch"   # or from a local checkout:
pipx install "/path/to/tools/litfetch[all]"

# or in a venv
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e "/path/to/tools/litfetch[all]"
```

Without extras the package still runs, with two degradations it will tell you about in `litfetch doctor`:

| extra | brings | without it |
|---|---|---|
| `pdf` → `pymupdf` | page count, text, title block | identity checks collapse to magic-byte + size |
| `browser` → `websocket-client` | the real-Chrome tier (L3) | paid/blocked publishers go to the manual tier |
| `tls` → `curl_cffi` | one retry with a Chrome TLS fingerprint | hosts that filter on TLS/UA answer 403 forever |

The agent skill ships **inside the wheel** — one version number for the CLI and the skill:

```bash
litfetch skill                 # write ~/.workbuddy/skills/lit-fetch/SKILL.md
litfetch skill --check         # exit 1 if the installed copy has drifted
litfetch skill --print         # stdout, for any other agent harness
```

No other state is created. `LITFETCH_MAILTO=you@example.com` is optional but recommended — Unpaywall
asks for an address, and the tier is skipped when it is unset.

---

## What this borrows, and from whom

Written in October 2026 by reading four open-source tools that solve overlapping problems. Each
borrowed idea is here because a specific failure preceded it, not because the tool is popular.

| idea | taken from | what it fixed here |
|---|---|---|
| **"honest results"**: report the route that produced a file and label it preprint / accepted manuscript / version of record | `fulltext-article-downloader` (computron) | `R141` was fetched from arXiv and stored under the journal article's name; four conditions passed and nothing recorded that the bytes are a preprint |
| **fail fast on a challenge or paywall page** instead of retrying it | `scihub-cli` (Oxidane-bot) | one batch hung for seven silent minutes retrying five URLs on two hosts that would never answer |
| **resolve a title to a DOI by score, and reject a weak match** (`--minimum-title-score`) | `DoiHunter` (mehmoodulhaq570) | the previous check compared the first 40 characters of the normalised titles, which is exactly the shape that accepts a sequel |
| **skip what is already there**, and report "already there" separately from "downloaded" | `DoiHunter`, `scihub-cli` | a batch printed `stored 4/4` over three files that were already in the library |
| **`Retry-After` and per-host pacing** | `scihub-cli`; Unpaywall/Crossref terms | no pacing at all; arXiv asks for three seconds and did not get it |
| **provenance in the item, not in an external database** (the reading-status idea) | Zotero `reading-list` / `Reading Flow` plugins | reading status was re-derived from filenames on every run, so a renumbered bibliography moved it silently |
| **a TLS-fingerprint retry as an optional extra, clearly labelled** | `fulltext-article-downloader`'s `[tls]` extra | see the measurement below |

Two things were deliberately **not** adopted: a Sci-Hub fallback (the package only fetches what the
network is already entitled to), and a headless-browser stealth stack. The measurement below is why the
second one is pointless here.

### Measured: a Chrome TLS fingerprint does not defeat Cloudflare

Seven references could not be fetched from a campus network with full entitlement. Re-probing them with
`curl_cffi` replaying Chrome's TLS handshake changed exactly one thing -- the failure got a better name:

| route | plain `urllib` | Chrome TLS fingerprint |
|---|---|---|
| Wiley → (now) Oxford Academic, DOI `10.1111/j.2517-6161.1993.tb01920.x` | 403 | **403, "Just a moment…"** |
| Project Euclid, `10.1214/aos/1176348248` | 200, 1 163-byte shell | **200, 1 163-byte shell** (`botwall`) |
| IEEE Xplore `stampPDF` | 502 | 502 |

Cloudflare's challenge is decided by JavaScript execution, not by the TLS record. So `tls` is wired as a
**retry that is skipped** whenever the diagnosis already says `cloudflare` / `botwall` / `challenge`, and
it is also skipped for HTTP 400 and for any HTML body over 32 KB — a full page is the site's answer, not
a fingerprint problem. The honest path for those hosts is one human download plus `litfetch import`.

---

## Quick start

```bash
litfetch doctor                                  # what this machine can do
litfetch verify paper.pdf --title "..." --year 2019
litfetch scan literature/                        # duplicates, non-PDFs, totals
litfetch diagnose --doi 10.1111/j.2517-6161.1993.tb01920.x --title "..."   # why it fails
litfetch resolve --doi 10.1214/aos/1176348248    # candidate URLs, in tier order
litfetch fetch refs.json --library literature/    # the tiered loop, verified
litfetch ledger --library literature/ --bibliography review.md --overrides reading_status.json
litfetch import ~/Downloads/paper.pdf --library literature/ --title "..." --year 1994
litfetch versions --library literature/          # preprint, accepted, or version of record?
litfetch discover --title "..." --web            # copies outside the OA indexes (author pages)
litfetch audit --library literature/ --bibliography review.md   # entry vs file, both ways
litfetch schema --write docs/record.schema.json  # the evidence-record schema
```

### "It did not download" is not a diagnosis

`diagnose` probes every candidate and names the blocker, because the action differs completely:

| verdict | what happened | what to do |
|---|---|---|
| `pdf` | the bytes are the paper | fetch would have stored it |
| `cloudflare` | "Just a moment…" interstitial | open in your browser, then `import` |
| `botwall` | Incapsula/Imperva/Distil/Akamai wall (a 1 KB iframe shell) | same |
| `challenge` | page asking to verify you are human | same |
| `paywall` | 401/402, or "purchase this article" | no entitlement here: proxy or ILL |
| `js_required` | 502 or an HTML shell | `--browser`, or import by hand |
| `dead` | 404/410 | find another copy |
| `not_pdf` | landing page, login redirect, TDM token error | read the page, then decide |

Real output from a campus network, where the entitlement was never in doubt:

```
A Fourier Approach to Nonparametric Deconvolution of a Density Estimate  (1993)
  not_pdf     400       0B  https://api.wiley.com/onlinelibrary/tdm/v1/articles/...
  cloudflare  403    5905B  https://academic.oup.com/jrsssb/article-pdf/...
  cloudflare  403    5787B  https://doi.org/10.1111/j.2517-6161.1993.tb01920.x
summary: bot wall -- a human browser is required
```

The second candidate is itself a finding: the DOI now routes to Oxford Academic, not Wiley.

`refs.json` is deliberately dumb:

```json
[{"num": "88", "title": "Efficient Coding of Subjective Value",
  "year": "2019", "authors": "Polania, Woodford, Ruff",
  "doi": "10.1101/358317", "urls": ["https://www.biorxiv.org/content/10.1101/358317v1.full.pdf"]}]
```

Every subcommand takes `--json` (where it makes sense) and returns a meaningful exit code:
`0` success, `1` verification failure, `2` environment problem. `litfetch doctor` exits `2` only when
the tool cannot verify anything at all (no PDF backend); a missing *optional* extra is a `!` warning
and still exits `0`, because the package is documented to run without it. Add `--strict` when you want
a minimal install to fail the gate. That makes it usable from CI:

```bash
litfetch scan literature/ || echo "the library has non-PDFs again"
```

---

## Layout: a pure half and a wired half

```
litfetch/
  core/   verify · provenance · store · ledger · model     no network, no optional dependency
  io/     sources · fetch · diagnose · browser             everything that reaches outside
  cli.py                                                   a thin shell over both
  skill/  SKILL.md                                         shipped in the wheel
```

The boundary is enforced, not aspirational: a test imports `core` in a fresh interpreter and fails if
`litfetch.io`, `pymupdf`, `curl_cffi` or `websocket` come along for the ride. CI repeats it with a job
that installs no extras.

`core/model.py` defines the **evidence record** — one JSON object per file, holding identity, version,
provenance, reading status and (later) screening decisions. `litfetch schema` prints the JSON Schema.
JSONL is the source of truth; a database, when there is one, is rebuilt from it.

## The four conditions

A candidate file is accepted only if all four hold:

1. **It is a PDF** — magic number, not the extension. A 1.2 MB ScienceDirect landing page is still HTML.
2. **It parses, has pages, and has text** (unless you pass `--require-text off` for a scanned paper).
3. **The title is in the front matter** — see below; this is the load-bearing check.
4. **The expected year appears in the front matter** (optional, catches reprints and sequels).

### Why "the title's words are present" is not enough

Three failure modes survived a word-bag check in practice:

| what happened | word coverage | why it still passed |
|---|---|---|
| a **sequel** paper, *Online Relational Inference…*, filed as *Neural Relational Inference…* | 1.00 | every word of the original title appears in the sequel |
| the **cited** paper's title printed in the abstract ("unlike *Neural Relational Inference*…") | 1.00 | the citing paper is itself a legitimate source of the words |
| **same first author**, different paper (PlaNet vs Director, both Hafner) | high | the author name check cannot see the difference |

So `litfetch` compares the expected title against **the largest-type lines of page 1** — the printed
title block — and requires a **contiguous run** of title words, not a bag:

```
coverage  = fraction of title words present anywhere in the first 3 pages   (reported)
adjacency = longest contiguous run of title words inside the title block    (enforced)
accept    ⟺ coverage ≥ 0.8 AND adjacency ≥ max(3, 0.6 × #title words)
```

Rotated margin stamps (`arXiv:…`), `doi:`/`issn:` lines and mostly-numeric lines are excluded before the
type-size comparison, otherwise an arXiv stamp wins the "largest type" contest on every preprint.

---

## The tier ladder

```
L0  arXiv / repository direct      resolve the ID from the title; never trust an ID from upstream
L1  Unpaywall + Crossref + OpenAlex  the publisher's own declared open location
LA  author page / arbitrary web    URLs a human or an agent found; tried before doi.org
L2  publisher direct + browser headers
L3  real Chrome over CDP            optional, opt-in (--browser)
L4  manual                          report honestly; do not lower the bar to make the count look good
```

Every tier's bytes go through all four conditions. A failure deletes the temp file and moves on;
nothing unverified is ever promoted into the library.

---

## Library conventions the tool enforces

* **Name**: `Author_Year_ShortTitle.pdf`, sanitised to be legal on Windows (no `: ? * " < > |`,
  no trailing dot, no `CON`/`LPT1`, ≤ 120 chars). The sanitiser is tested, because a library that
  cannot be copied to a Windows machine is not portable.
* **Identity**: files are de-duplicated by **SHA-256 of the content**, never by name.
* **Quarantine, never delete**: anything that fails goes to `_misfiled/` with a line in
  `_misfiled/MANIFEST.jsonl` saying what the file really was. The audit trail is the product.
* **Ledger**: `litfetch ledger` classifies every PDF as `A` (full read), `B` (abstract-level skim),
  `C` (never read) or `X` (unusable), by matching file names against a bibliography's
  `(surname, year)` pairs. Non-mechanical matches live in an overrides JSON, next to the bibliography.
  `--write FILE` replaces only the region between `<!-- BEGIN GENERATED:reading-status -->` and
  `<!-- END GENERATED:reading-status -->`, so prose and counts can share one file without either
  overwriting the other.

The distinction the ledger exists to make: *having the file* and *having read the file* are different
states, and a project that does not record the difference will eventually cite something it never opened.

---

## Tests

```bash
python tests/run_tests.py       # dependency-free runner, 101 tests
python -m pytest -q             # also works if pytest is installed
```

The suite covers the four conditions (including the sequel and the landing page), Windows-safe naming,
hash de-duplication, the quarantine manifest, ledger classification, provenance labels, the tier order,
fail-fast per host, the year rule, the record schema, the core/io boundary, and the skill/tool
round-trip (`skill` → `--check` must report identical). Many tests exist because the code was wrong first: a source file
inside the library matched itself during de-duplication; `[A-Za-z]` silently truncated `Schölkopf` to
`Sch`; a verbatim title match scored a *different* paper 1.00; and a provenance key built from the file
name merged two layers into one.

---

## When no tier works: hand it to a human, then `import`

Some publishers cannot be passed by any script. Measured on a campus network (entitled IP, 2026-10-22):

| publisher | scripted GET | headless Chrome |
|---|---|---|
| Wiley | 403 | lands on Cloudflare "Just a moment…" |
| IEEE Xplore | 502 | JS session required |
| Project Euclid | 200 but a 1.2 KB challenge page | same |
| JSTOR | blocked | — |

**The block is bot detection, not entitlement** — which is why a campus network does not help a script.
The honest path is one human download, then:

```bash
litfetch import ~/Downloads/saved.pdf --library literature/ --layer L14 \
    --title "Exact and ordinary lumpability in finite Markov chains" --year 1994 --authors Buchholz
```

`import` applies the same four conditions, refuses a mismatched file (exit 1), and only then names and
stores it. `--force` sends a failing file to `_misfiled/` with the reason recorded instead of pretending.

**A whole folder at once.** Save five PDFs with meaningless names; the matcher compares each file's
title block against `refs.json` and imports the ones it can prove:

```bash
litfetch import --from-dir ~/Downloads/papers --refs refs.json \
    --library literature/ --layer L14
# stored     download_1.pdf   .../Michel_2024_Formal_error_bounds_....pdf
# unmatched  download_2.pdf   no reference title matches this file
```

An unmatched file is left alone. Guessing is how a library ends up with the wrong paper under the right
name, which is the exact failure this package exists to prevent.

## Ids are cheap to check and expensive to trust

Every DOI or arXiv id that arrives from outside is checked against the expected title *before* a
download is attempted (`verify_id`). This is not theoretical: two ids recalled from memory during the
2026-10 audit (`1802.05438`, `1812.11103`) pointed at *Mean Field MARL* and *Learning to Walk* instead of
QMIX and PlaNet. `diagnose` prints a warning when an id and a title disagree; `fetch` refuses to treat the
id's own metadata as confirmation.

## Papers that exist only on the author's own page

OpenAlex and Unpaywall index repositories, not personal homepages, so a large class of older papers
looks "unavailable" to a script while being one search away. The **LA tier** covers it: candidate URLs
found by a human or an agent are tried *before* `doi.org`, because for a paywalled paper the author's
copy is more likely to be reachable than the publisher's.

```bash
litfetch discover --title "On the Optimal Rates of Convergence for Nonparametric Deconvolution Problems" \
    --doi 10.1214/aos/1176348248 --web
#   botwall        1054B  http://projecteuclid.org/download/pdf_1/euclid.aos/1176348248
#   botwall        1161B  https://projecteuclid.org/journals/annals-of-statistics/volume-19/...
#   note: search unreachable (URLError)
```

`discover` probes every candidate and prints a ready-to-paste `urls` block for `refs.json`:

```json
{"num": "R143", "title": "...", "year": "1991",
 "urls": ["https://archive.ymsc.tsinghua.edu.cn/pacm_download/468/10809-Jianqing_Fan_7.pdf"]}
```

`fetch` then tries that URL at tier `LA`, with the same four conditions as any other candidate. **This is
what makes a "wild web" tier safe**: a wrong hit costs one download, never a wrong citation.

### What the first run of this tier actually produced

`discover` cannot reach a search engine from every network (measured here: the proxy refuses
`html.duckduckgo.com` with a 502, and the command says so instead of returning silence). Supplied by hand,
the route immediately found a real copy of R143 on the author's own archive — and verification then
rejected it as the 1991 article:

```
front matter carries 1977, 1980, 1982, 1987 but not 1991 -- a different version of the same title
first line: ... Technical Report No. 157, June 1988, University of California, Berkeley
```

It is the 1988 technical report of the same paper: same title, same author, different document. It is now
stored under its own name (`Fan_Jianqing_1988_...`) and labelled `preprint`; the 1991 article is still
missing. **The route works, and the check is what keeps it honest** — an exact title match is precisely
what makes a version substitution invisible, which is why the year keeps its veto.

## Identity is not version

Four conditions prove a file is *the paper*. They say nothing about *which version* of it:

```bash
litfetch versions --library literature/
# tracked 159 of 159 files
#   vor       0
#   accepted  3
#   preprint  53
#   unknown   103
#   159 records carry no download URL: their version was inferred from page 1 only, ...
```

Every stored file gets a line in `<library>/PROVENANCE.jsonl`:

```json
{"file": ".../L14/Michel_2024_....pdf", "sha256": "…", "retrieved": "2026-10-22T02:31:11+00:00",
 "url": "https://arxiv.org/pdf/2403.07618", "tier": "L0", "transport": "plain",
 "version": "preprint", "version_basis": "page 1 says \"arXiv:2403.07618\"",
 "title_declared": "Formal error bounds …", "title_printed": "Formal Error Bounds …",
 "coverage": 0.94, "pages": 44}
```

The label is always accompanied by its evidence (`version_basis`), and `vor` from a publisher's host is
worded as **asserted**, not verified — nobody compared it with the journal's own PDF. `--version-required
vor` makes `fetch` refuse a preprint outright.

`--backfill` labels files that predate the record. Expect a lot of `unknown` there, and that is the
correct answer: six publisher-looking PDFs sampled in this library carried no DOI, no volume/page line
and no copyright notice on page 1 — they were arXiv-style downloads of published papers. You cannot
recover the version from the bytes; you can only record it at download time.

## Audit: the question you actually have before submitting

```bash
litfetch audit --library literature/ --bibliography review.md --report reports/audit.md
litfetch audit --library literature/ --zotero ~/Downloads/My\ Library.json   # CSL-JSON or BibTeX
```

Every entry goes one way, every file the other. The verdicts are deliberately **not** symmetric, because
"the check failed" is four different situations:

| verdict | meaning | what to do |
|---|---|---|
| `verified` | paired, title block agrees | nothing |
| `other-version` | same title, a different year in the front matter | cite the version you hold, or fetch the journal one |
| `mismatch` | title disagrees beyond layout noise | **find the right file** |
| `check-failed` | the title survives, the extractor did not (kerning, two-column) | open it once |
| `unverifiable` | no text layer, or the entry carries no title | out of scope for a machine |
| `not-a-pdf` | a landing page or stub | quarantine |
| `duplicate` | the same work already has another key in the list | fix the bibliography |
| `full-text-only` | held as text (txt/md/XML), never as PDF | don't fetch; there is nothing to fetch |
| `missing` / `orphan` | entry without file / file without entry | to-do list / library hygiene |

Measured on this project's own review (131 entries, 160 files):

```
verified 77   other-version 19   mismatch 0   check-failed 0   unverifiable 5
duplicate 18  full-text-only 2  missing 10   orphan 59        (190 rows)
```

**The first run said `missing 41`, and 31 of those were never missing.** Eighteen were the same work
under a second key (the review absorbed a citation map whose rows overlap its own), eleven had a file
the naming rule could not pair, and two were held as full text. Reporting those as *missing* was a
statement about the file names dressed up as a statement about the library — the failure this package
exists to prevent, one level up. `literature/aliases.json` now carries the 31 settled rows and the
verdict follows the content.

Two things worth reading from that. **`mismatch 0`** is not luck: the wrong files this library used to
have were found and fixed earlier the same day, and this run is the check that they stayed fixed.
**`other-version 18`** is the price of citing a bibliography: eighteen claims would rest on a preprint
while the reference points at a journal.

### When the name does not follow the rule

Pairing is by `(surname, year)` because that is the library's naming rule. Real
libraries break it: `VisualPredicator_ICLR2025.pdf` has no surname at all, the
Cyrillic entry `Колмогоров` meets the Latin file `Kolmogorov_1965_...pdf`, a
preprint is one year off the journal, and a file sent to quarantine is skipped by
`pdfs()` on purpose — so its entry stayed `missing` forever and *"I set this
aside"* was reported as *"I never had it"*.

```json
{"entries": {
  "R6":   {"file": "L4_.../VisualPredicator_ICLR2025.pdf", "why": "code-named system"},
  "R105": {"duplicate_of": "R95", "why": "same Rainforth 2024 paper"},
  "R143": {"file": "_misfiled/Fan_..._1991_....pdf", "why": "quarantined: it is TR 157 (1988)"},
  "R11":  {"full_text": "L1_.../Hoel_2013_..._全文存档.md", "why": "PNAS PDF behind Cloudflare"}
}}
```

A row without `why` is **refused at load time**, and the file it names is still
verified — an alias says where to look, never what the answer is. `R143` comes
back `other-version` rather than `verified` precisely because the alias let the
year rule see the 1988 report behind the 1991 name.

### Letter-spaced titles

Kolmogorov 1965 sets its title as `Т Р И  П О Д Х О Д А  К  О П Р Е Д Е Л Е Н И Ю`:
one letter per token, so every word-level test scores zero. When the word-level
check fails, `verify` asks one more question — is the *entire* title, spaces
removed, a contiguous substring of the front matter? Containment, not overlap:
the ORI sequel still fails that test, which is why it is allowed to exist.

### It got there by being wrong first

The first run reported **27 mismatches**. Eighteen were the right paper in another version, four were
scans with no text layer, one was the HTML stub above. Only after splitting those out did the remaining
quirks show up — an Elsevier "Accepted Manuscript" cover sheet whose largest type is the label, a
`Toward`/`Towards` spelling variant failing the short-title rule, and a bioRxiv title kerned into
`GE M OT`. Each is now handled and tested.

The risk in that sequence is obvious: a classifier loosened until nothing is flagged. So the fixture
contains a **known wrong pair** — the "Online Relational Inference" sequel filed under "Neural
Relational Inference" — and it must keep coming out as `mismatch`. It is also why the fallback is a
*squashed prefix* test rather than n-gram overlap: that pair shares 0.79 of its 4-grams, and a
containment test is what separates "kerning" from "another paper".

## Long batches, resumed batches

```bash
litfetch fetch refs.json --library literature/ -v \\
    --timeout 25 --budget 90 --log /tmp/attempts.jsonl --resume

litfetch fetch refs.json --library literature/ --skip-existing   # name already present
```

* `--budget` caps the wall clock **per reference** and skips the remaining candidates — but not the
  metadata lookups that happen before the first candidate, so a batch can overshoot by the resolution
  time of its last few references;
* `--resume` reads `--log` and skips references already `verified` there;
* `--skip-existing` skips a reference whose conventional filename is already in the library;
* `--log` appends one JSON line per attempt; the run summary goes to `<log>.summary.json`, never on top
  of the log (an earlier version overwrote the log at the end of every successful run).

## Reading the end of a run

A batch of 125 refuses to be read one line at a time, so the run ends with the *distribution* of
failures and the one action that follows from each:

```
stored 60  duplicate 1  skipped 0  blocked 7  rejected 11  unavailable 46

why 64 of 125 were not stored:
    23  publisher page needs JS        retry with --browser, or import by hand
    11  rejected: page-1 identity      open it; usually another version or a same-title paper
     7  bot wall (Cloudflare)          open in your own browser, then `litfetch import`
     6  landing page, not a PDF        supply the PDF URL; abstract pages rarely serve one

not attempted:
  L1    Unpaywall has no contact address, so it contributed no candidates at all
          fix: export LITFETCH_MAILTO=you@example.org
  L3    Chrome tier not enabled
          fix: add --browser
```

The second block is the one that matters most in practice: a silently skipped tier looks exactly
like "no open copy exists". `--json` prints the same report as an object on stdout (human lines move
to stderr, so `| jq` works) for callers that want to branch on `reasons[].reason`.

`--only` accepts ranges and globs, and refuses to run on a typo:

```bash
litfetch fetch refs.json --library literature/ --only L001-L020      # a block
litfetch fetch refs.json --library literature/ --only 'COGR-L*'      # a family
litfetch fetch refs.json --library literature/ --only L01            # exit 2, with suggestions
```

## Limits, stated plainly

* Front-matter checks assume a normal typeset PDF. Scanned files have no text, so identity is
  **not** verified; the tool says so instead of pretending.
* Non-Latin titles work (the normaliser keeps CJK), but the adjacency threshold assumes whitespace
  tokenisation.
* The browser tier is the least portable part: it needs Chrome/Chromium/Edge and a socket library, and
  it is the only place where failure modes are environmental rather than logical.
* `L3` exists for publishers that reject scripted requests. It is not a paywall circumvention tool:
  it fetches what a browser on *your* machine with *your* entitlements can already open.
* **Content-hash de-duplication only catches byte-identical files.** Two downloads of the same paper
  from two routes usually differ by an embedded creation timestamp, so `duplicate` is rarer than one
  expects; identity-level de-duplication is the ledger's job, not the store's.
* **`version` is a label with a reason, not a proof.** `preprint` and `accepted` are read off page 1 or
  the host; `vor` from a publisher's host is *asserted*. A citation that depends on the exact version
  should still open the file.
* **A failed identity check outranks a failed download** in the status column: `rejected` means a file
  arrived and was the wrong paper, `blocked` means the host refused to serve one at all.
* **The search-engine tier is the least reliable and the most useful.** It is off unless `--web` is
  given, it frequently fails (proxies, bot checks), and it reports that failure rather than returning an
  empty list. Supplying the URL by hand is a first-class path, not a workaround: `discover` exists to turn
  a human's two-minute search into a verified library entry.
* **The TLS retry is a fingerprint, not an authorisation.** It never logs in, presents a token, or uses
  a proxy. It changes how the request looks, which is worth checking against your institution's terms.
* **Content-hash de-duplication only catches byte-identical files.** Two downloads of the same paper
  from two routes usually differ by an embedded creation timestamp, so `duplicate` is rarer than one
  expects; identity-level de-duplication is the ledger's job, not the store's.
* **`version` is a label with a reason, not a proof.** `preprint` and `accepted` are read off page 1 or
  the host; `vor` from a publisher's host is *asserted*. A citation that depends on the exact version
  should still open the file.
* **A failed identity check outranks a failed download** in the status column: `rejected` means a file
  arrived and was the wrong paper, `blocked` means the host refused to serve one at all.
* **The TLS retry is a fingerprint, not an authorisation.** It never logs in, presents a token, or uses
  a proxy. It changes how the request looks, which is worth checking against your institution's terms.
