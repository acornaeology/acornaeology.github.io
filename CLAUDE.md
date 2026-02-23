# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build

```bash
uv run python -m generator.build
```

This cleans `output/`, copies static assets from `site/`, renders Jinja2 templates, and generates disassembly pages from JSON data. Output goes to `output/` (gitignored). Deployed to GitHub Pages via `.github/workflows/deploy.yml` on push to `master`.

No test suite exists.

## Architecture

Static site generator for annotated 6502 disassemblies of Acorn ROMs. Transforms JSON disassembly data into interactive HTML pages with syntax highlighting, cross-references, and responsive layout.

### Data flow

1. `data/sources.json` lists external disassembly repos (local paths or GitHub URLs, cloned to `.cache/`)
2. Each source repo has `acornaeology.json` manifest, `versions/*/rom/rom.json` metadata, and `versions/*/output/*.json` disassembly data
3. `generator/build.py` orchestrates the build: loads sources, renders static pages, calls `generator/disassembly.py` for each disassembly
4. `generator/disassembly.py` converts JSON items into template-ready line dicts with pre-rendered HTML (`Markup` objects)
5. `templates/_disassembly.html` renders the two-column layout (subroutine nav + listing table)

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
