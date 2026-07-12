# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build

```bash
uv run python -m generator.build
```

This cleans `output/`, copies static assets from `site/`, renders Jinja2 templates, and generates disassembly pages from JSON data. Output goes to `output/` (gitignored). Deployed to GitHub Pages via `.github/workflows/deploy.yml` on push to `master`.

## Tests

```bash
uv run pytest
```

Tests live in `tests/` and cover the renderer helpers in `generator/`. The
deploy workflow runs `pytest` before building, so a test failure blocks the
GitHub Pages deploy — run the suite before pushing.

## Architecture

Static site generator for annotated 6502 disassemblies of Acorn ROMs. Transforms JSON disassembly data into interactive HTML pages with syntax highlighting, cross-references, and responsive layout.

### Data flow

1. `data/sources.json` lists external disassembly repos (local paths or GitHub URLs, cloned to `.cache/`)
2. Each source repo has `acornaeology.json` manifest, `versions/*/rom/rom.json` metadata, and `versions/*/output/*.json` disassembly data
3. `generator/build.py` orchestrates the build: loads sources, renders static pages, calls `generator/disassembly.py` for each disassembly
4. `generator/disassembly.py` converts JSON items into template-ready line dicts with pre-rendered HTML (`Markup` objects)
5. `templates/_disassembly.html` renders the two-column layout (subroutine nav + listing table)

### Schema versions

Each disassembly JSON carries `meta.schema_version` (integer). The generator
supports v2, v3, and v4 concurrently, gating on this value (default 2 when
absent) — **not** on the presence of any particular key — because sibling repos
upgrade dasmos on independent schedules. The expression gate lives in
`process_disassembly` (`disassembly.py`), which reads the version and threads an
`ExprContext` down to the byte/word renderers.

- **v2:** `expressions[i]` is a bare display string.
- **v3:** `expressions[i]` (and a code item's `expr`) is a `{"text", "tree"}`
  object. Display uses `text` (precedence-safe beebasm); `tree` linkifies label
  refs (`ref`/`name` → on-page anchor) and macro invocations (`macro_call` →
  the `#macro-NAME` definition in the macros section). The v3 top-level `macros`
  section renders via `build_macros`. Unknown `tree` node kinds degrade to
  `text` — never a crash or a dict repr. `_expr_parts` defensively unwraps a
  dict even under the v2 gate, so a mislabelled document still can't leak a repr.
- **v4:** changes the **memory map** only (expressions/macros unchanged from
  v3). `memory_map[].access` is an orthogonal flag **list** — a subset of
  `["r", "w", "b"]` (read / write / indexing base) — replacing the v2/v3
  mutually-exclusive scalar (`r`/`w`/`rw`). Indexing bases, which v2/v3 emitted
  in a separate top-level `index_bases` array, are folded into `memory_map` as
  `["b"]` rows so they sit in place within their group. `_normalized_memory_map`
  (`build.py`) reconciles both shapes: v4 passes through; v2/v3 has its scalar
  `access` widened to a list and its `index_bases` appended as `["b"]` rows. So
  the memory-map renderer and the listing's operand-link wiring have a single
  code path. See dasmos `docs/design/json-schema-v4.md` and issue #42.

### Disassembly-page link categories

The links at the top of a disassembly page are grouped by provenance so
research-source attribution is explicit — our own artefacts vs the ROM's
identity vs the upstream work we consulted vs discussion. `LINK_CATEGORIES` in
`build.py` defines the ordered categories and their headings; `_group_links`
buckets the flat list and omits empty groups:

- `ours` — **This disassembly**: GitHub source, memory map, generated companion
  doc pages.
- `rom-image` — **ROM image**: the ROM's entry in tobylobster's ROM Library
  (which exact binary, by md5).
- `source` — **Sources consulted**: prior disassemblies / reconstructions / docs
  that informed the annotations.
- `related` — **Further reading**: relevant but not directly consulted.
- `discussion` — **Discussion**: forum threads.
- `feedback` — **Feedback**: the report-an-issue link (visually demoted).

A version may ship its disassembly in more than one assembler flavour. List
them under `sources` in `rom.json` (each `{"assembler", "url"}`) and they render
as a single "This disassembly" line — `Disassembly source on GitHub (beebasm,
64tass)` — with each flavour linked (`_build_source_link`). A repo with a single
flavour can keep its legacy single github link in `links`; both forms are
supported.

Each link may declare `category` explicitly; when absent it is inferred from its
`icon` (`_ICON_LINK_CATEGORY`), so un-migrated repos still group correctly. The
consulted-vs-further-reading split is an authorship judgement — inference
defaults a referenced disassembly/doc to `source` (consulted); set
`category: "related"` to mark it as further reading. The toolchain is credited
site-wide in the footer (`base.html`).

**Where references live.** Research sources are curated once per repo in the
`references` array of `acornaeology.json` (repo-level, each with a `note`, a
`category`, and an optional stable `id`). They render grouped on the project
index page *and* are merged into every disassembly page's link groups.

A version's `rom.json` may carry its own `references` array to **specialise** or
**enrich** the shared list (`_merge_references`): an entry whose `id` matches a
shared reference replaces it in place (e.g. ANFS 4.21, Master-only, swaps the
shared Model-B MOS disassembly for the Master one via `id: "mos"`); `suppress:
true` removes a shared entry; an entry with a new/absent `id` is appended.
`_dedup_links` then drops any entry a version-specific `rom.json` link already
supplied (e.g. a shared discussion thread). So `rom.json` `links` should carry
only version-specific *links* (the `sources`, the ROM-image `chip`, a
version-specific thread); shared research references belong in the manifest, and
`rom.json` `references` only the per-version overrides/additions.

### Glossary

`generator/glossary.py` parses `GLOSSARY.md` from source repos. Each entry uses the Pandoc multi-paragraph definition list convention to encode brief and extended descriptions:

```
**TERM** (Expansion)
: Brief definition — one or two sentences. What the term IS.

  Extended detail — how NFS uses it, implementation specifics,
  or additional context. Shown only on the glossary page.
```

First paragraph (the `: ` line and its continuations before any blank line) = **brief**, used for tooltip text in doc pages. Subsequent indented paragraphs after a blank line = **extended**, shown only on the glossary page. Entries without extended detail keep a single paragraph.

Doc pages link terms to the glossary via `glossary_links` in `rom.json`. These are applied post-HTML-conversion (unlike `address_links` which are pre-conversion) to avoid wrapping text already inside `<a>` elements.

### Key constants

- `CONTENT_MAX_WIDTH = 64` in `disassembly.py` — maximum character width for all content lines (code, data, comments are wrapped/grouped to fit)
- Address column is 4 hex chars + 1.5em padding; total listing width ≈ 70.5 monospace characters

### CSS

- Light/dark theming via CSS custom properties and `prefers-color-scheme`
- Tooltips use `data-tip` attribute + CSS `::after` pseudo-element (not native `title`)
- Responsive breakpoint at 900px: sidebar stacks above listing on mobile, collapses via `<details>`
- Listing font scales to fit viewport on mobile: `calc((100vw - 3rem) / 42.3)`
- Come-from reference popups on labels with `references` data

### Naming conventions

Use suffixes `_filename`, `_filepath`, `_dirpath`, `_dirname` — not ambiguous `_dir` or `_file`.
