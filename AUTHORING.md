# Acornaeology authoring guide

This document describes the features the shared toolchain (py8dis
fork + site generator + asm verification harness) now provides, and
the conventions drivers and analyses in sister-repo disassemblies
(`acorn-nfs`, `acorn-adfs`, `acorn-6502-tube-client`, and future
siblings) should adopt to benefit from them. The
`acorn-econet-bridge` repo is the reference implementation — if
anything here is unclear, look there for a worked example.

The guide assumes you are working inside one of the sibling
disassembly repos. Cross-repo dependencies (the `py8dis` fork at
`/Users/rjs/Code/acornaeology/py8dis`, the site generator at
`/Users/rjs/Code/acornaeology/acornaeology.github.io`) are pinned
through each sister's `uv.lock` / `pyproject.toml`.

---

## 1. Inline-comment Markdown

Comments you write with `comment(addr, text, inline=True|False)`
are parsed as Markdown by both the asm output path and the HTML
listing path. The asm path collapses to plain text; the HTML path
renders the Markdown.

### 1.1 Address references

Write cross-references using the explicit Markdown link form:

```python
comment(0xE041, "Mismatch → [`ram_test_fail`](address:F28C)",
        inline=True)
```

Supported target forms:

- `address:HEX` – same-version reference (the default when no
  version is qualified).
- `address:HEX@VERSION` – cross-version reference (mainly used in
  project-level analyses that talk about multiple ROM versions).
- `address:HEX?hex` – appends a second hyperlink ` (&HEX)` so the
  label and the hex are both clickable. Use for the first mention
  of a routine in a prose block.

Resolution (HTML pipeline):

1. If `HEX` is a memory-map entry (a `label()` address with
   memory-map metadata – see §4), the link points to the
   memory-map page in a named side window (`target="memory-map"`).
2. Otherwise, if `HEX` is a ROM-range anchor, it points to the
   same-page `#addr-HEX`. Addresses that fall *inside* a
   multi-byte item resolve to the nearest preceding anchor.
3. Otherwise the link is left unresolved and a build-time warning
   is printed.

### 1.2 Bare `&XXXX` is no longer auto-linked

Prior versions would silently wrap any bare `&XXXX` in a listing
comment with an anchor. That convenience is gone – if you want a
link, write `[label](address:HEX)` explicitly. Any remaining bare
`&XXXX` renders as plain text (still perfectly readable; just not
clickable).

When sweeping a legacy driver, expect to rewrite every occurrence
of ``… `xxx` at `&HEXX` …`` into ``… [`xxx`](address:HEXX?hex) …``.

### 1.3 Inline Markdown primitives

Inside comments and descriptions you can use:

- **Bold** via `**` – renders visually bold in HTML, dropped in asm.
- *Emphasis* via `*` – same treatment.
- `Inline code` via backticks – renders monospace in HTML, strips
  backticks in asm.
- Backslash-escapes for literal `&`, `[`, `]`, `|`, etc. when you
  need to stop Markdown from interpreting them.

### 1.4 Typography (Unicode, not ASCII)

Use the proper Unicode character rather than an ASCII construct:

| Write                                           | Not             |
|-------------------------------------------------|-----------------|
| ` → `                                           | ` -> `          |
| ` ← `                                           | ` <- `          |
| ` – ` (U+2013 en-dash, with surrounding spaces) | ` -- ` or ` — ` |
| `…`                                             | `...`           |

Use **en-dash** (` – `) for parenthetical breaks. Avoid the
American-style close-up em-dash (`—`).

Operator tokens in prose (`<=`, `!=`, `>=`) stay as ASCII – they
overlap with Python syntax in the driver and the wins are not
worth the discrimination cost.

beebasm reads source files in binary and jumps past comment bodies
on `;` / `\`, so UTF-8 in comments is completely invisible to the
assembler. Round-trip verify still passes.

---

## 2. Block Markdown in subroutine descriptions

`subroutine(addr, name, title=…, description=…)` accepts full
CommonMark plus GFM tables and fenced code. Use this to give each
routine a readable summary on the HTML listing's subroutine header.

### 2.1 Structural features

- **Paragraphs** separated by blank lines.
- **Bulleted / numbered lists.** Nested lists supported.
- **Tables** with the GFM pipe syntax:

      | Column A | Column B |
      |----------|----------|
      | ...      | ...      |

  Tables render with subtle column borders and wrap cell content
  when they exceed the description width.

- **Fenced code blocks** with a language tag:

      ```6502
      LDA #&55
      ```

  Emits `<pre><code class="language-6502">` so a future
  highlight.js pass can hook in.

- **Inline code** with backticks (`like this`). Use for register
  names, address literals, protocol tokens.

### 2.2 Address references in descriptions

Use `[label](address:HEX[?hex])` exactly as in inline comments
(§1.1). On the HTML listing the link scrolls to the anchor; on the
memory-map page, the same syntax resolves cross-window into the
listing via `target="listing"` so the reader can keep both windows
side by side.

### 2.3 Titles

The `title=` kwarg is a short one-line banner shown in the listing
and used as the tooltip when the reader hovers any `JSR / JMP /
BXX` operand targeting the routine. Keep titles terse (≤ 60 chars
is a good target) and self-contained – they do double duty as
tooltip text.

---

## 3. Memory map

Non-ROM addresses (zero page, RAM workspace, RAM buffers, MMIO
registers) can be enriched with metadata so they surface as
first-class entities on the site.

### 3.1 `label()` kwargs

```python
label(0x0080, "mem_ptr_lo",
    description="Low byte of the indirect pointer.\n"
                "Paired with [`mem_ptr_hi`](address:0081).\n\n"
                "Used by [`ram_test`](address:E00B) to scan memory "
                "upward one page at a time.",
    length=1, group="zero_page", access="rw")
```

- `description` – full Markdown. First sentence (see §3.2) is the
  tooltip / asm trailing-brief; first paragraph + extended
  paragraphs render in full on the memory-map page.
- `length` – bytes covered (1 for scalars; larger for buffers
  like 256-byte routing tables). Drives the `&XXXX–&YYYY` range
  display on the memory-map page.
- `group` – bucket for the memory-map page sections. Pick from
  `zero_page`, `ram_workspace`, `ram_buffers`, `io_a`, `io_b`,
  `io`, `mmio`. New groups are allowed – they'll render as a
  section with a Title-Cased version of the key.
- `access` – one of `r`, `w`, `rw`. Renders as `R` / `W` / `R/W`.

Any `label()` with at least one of these kwargs appears in the
memory-map JSON and on the memory-map page. Plain `label(addr,
name)` calls without metadata behave exactly as they always did
(equate only, no memory-map entry).

### 3.2 Tooltip-boundary convention

py8dis derives a short **brief** from the first paragraph of the
description for use in tooltips and the asm trailing comment:

- Split on the first `\n\n` to get the first paragraph.
- Split on the first `\n` *inside* the first paragraph to get the
  tooltip brief; the rest of the first paragraph still renders on
  the memory-map page (the single `\n` is a Markdown soft break
  that joins with a space in rendered output).
- Markdown stripping (backticks, address links) runs after the
  split, so the brief drops cleanly into a `data-tip` attribute.

Use the `\n` marker when the first paragraph has more than one
sentence and the leading sentence suffices on its own for the
tooltip:

```python
description=(
    "RX frame buffer byte 4 – control byte.\n"      # ← tooltip ends here
    "Bridge protocol uses `&80`..`&83`; see "
    "[`rx_frame_a_dispatch`](address:E14A)."
),
```

Single-sentence first paragraphs need no `\n` – the whole paragraph
is the brief.

### 3.3 Cross-references between memory-map entries

Inside a memory-map description, `[label](address:HEX)` pointing
at another memory-map entry resolves to the in-page `#mm-NAME`
anchor on the memory-map page itself (no cross-window hop).
Addresses pointing into ROM resolve cross-window into the listing.

### 3.4 Self-describing tooltips

Remember these descriptions will render as tooltips when readers
hover operand labels in the listing. Write the tooltip sentence to
stand alone – don't rely on cross-referenced context:

> ✗ `description="As [`adlc_a_cr1`](address:C800) but for ADLC B.\n…"`
>
> ✓ `description="ADLC B control/status port 0.\nWrite: CR1…"`

A tooltip that just says "see other thing" is useless to the reader.

---

## 4. Fill regions and banners

### 4.1 `fill(addr, n)`

Regions of identical bytes (typically `&FF` padding in a ROM tail)
collapse to a single `for … next` line in the asm output. Use
`fill()` to mark them; the round-trip verify still expects the
exact byte pattern.

### 4.2 Subroutine-style banners on data

`subroutine()` can be used on data addresses too, deliberately, to
put a visible section break in the HTML listing between large fill
regions, the hardware vector table, or any other landmark. Pick a
name that makes sense as a heading (`rom_body_gap`,
`hardware_vectors`, etc.) and write a description that explains
what the region is and why it's there.

---

## 5. Analyses and writeups

### 5.1 Per-project analyses

Analyses are Markdown files referenced from the repo's
`acornaeology.json` under `analyses`. They render as standalone
pages under `/{slug}/{stem}.html` and appear in the project
index's "Analyses" section.

```json
{
  "analyses": [
    {
      "label": "Architecture overview",
      "url": "docs/analysis/bridge-architecture-overview.md",
      "icon": "doc",
      "note": "Top-down tour of the whole firmware…"
    }
  ]
}
```

Analyses use the same `[label](address:HEX)` syntax. For
references across versions inside an analysis, qualify with
`@VERSION` (for multi-version projects):

```markdown
`ram_test` at [`&E00B`](address:E00B@1?hex) behaves differently
from the same slot in [`&E00B` on 3.60](address:E00B@3.60).
```

Unqualified `address:HEX` defaults to the `default_version` the
analysis was rendered with (specified per-project in the manifest).

### 5.2 Per-version `docs` (release notes, change logs)

Each version's `rom.json` has an optional `docs` array. Entries
with `"type": "changes"` appear in the project-index table under
the **Changes** column; other doc types render as standalone pages
linked from that index row's doc list.

```json
"docs": [
  {
    "type": "changes",
    "label": "Changes from NFS 3.40",
    "path": "CHANGES-FROM-3.40.md"
  }
]
```

### 5.3 Glossary

Each project can supply a `GLOSSARY.md` registered via
`acornaeology.json`'s `glossary` key. Syntax:

```markdown
**TERM** (Optional expansion)
: Brief definition – one or two sentences describing what the
  term *is*. Shown in tooltips.

  Extended detail across multiple paragraphs. Only shown on the
  glossary page, not in tooltips.
```

Entries appear on the generated glossary page and are
tooltip-attached to matching terms in writeups (via the
`glossary_links` map in `rom.json` / `acornaeology.json`, checked
at lint time).

---

## 6. Tone, voice, and sourcing

### 6.1 Own the claim; don't narrate the process

Published prose should present findings, not the authoring
journey:

- ✗ "I originally mis-labelled this routine `foo_bar` before
   realising it was actually `foo_baz`…"
- ✓ "`foo_baz` … (earlier drafts used the name `foo_bar`, which
   reflected its use in a single caller rather than its general
   role)."

Keep internal-monologue commentary out of committed descriptions
and analyses – it's conversation context, not published content.
Search for first-person leaks (`I `, `I've`, `my `, `our `, `we
decided`, `originally`, `mis-`) before committing an analysis.

### 6.2 External sources (J.G. Harston, etc.)

When a published cross-reference informs a claim, adopt its name
where you've independently verified it matches. Where it diverges,
weigh each interpretation against the evidence and present your
conclusion in your own words. Paraphrase, don't copy. Soften
language like "JGH proved…" to "JGH's reading is… and that
aligns with what we observe at &XXXX". Don't copy the source's
tone, naming idioms, or phrasing verbatim.

### 6.3 Stylistic preferences

- Use space-en-dash-space (` – `) rather than close-up em-dash
  (`—`) for parenthetical breaks.
- Full hex addresses as `&XXXX` (uppercase, four digits).
- Use "the Bridge" / "the ADLC" / "the ROM" with capital first
  letter when referring to the system under study, lower-case for
  generic references.

---

## 7. Site-generator behaviours worth knowing

### 7.1 The two-window pattern

Clicking a memory-map reference in the listing opens the memory-
map page in a window named `memory-map`. Clicking a code reference
in the memory-map opens the listing in a window named `listing`.
Subsequent clicks reuse the same named window, so a reader can put
the two side by side and navigate freely between them.

Both templates include a small click-focus helper so the partner
window comes to the front rather than lingering behind the current
one. Modifier-clicks (Cmd/Ctrl/Shift, middle-button) fall through
to the browser's default new-tab behaviour.

### 7.2 Tooltip taxonomy

`data-tip` attributes drive CSS tooltips. Format is consistently
`&XXXX – text`:

- Operand labels targeting a memory-map entry → `&XXXX – brief`
  (from the memory-map description).
- Operand labels targeting a ROM subroutine → `&XXXX – title`
  (from the subroutine's `title=` kwarg).
- Operand labels targeting anonymous ROM items → `&XXXX`.
- Immediate literals (`#&55`) → a breakdown (decimal, hex,
  binary).

### 7.3 Colour convention

- Cyan (`--cyan`) = memory-location reference (`.mm-link`).
- Magenta (`--operand-link`) = code / jump-label reference.
- Labels at column 0 are bold magenta to match their references.
- Immediate literal backgrounds and other decorations are defined
  in the main stylesheet; don't override locally.

Picking a consistent colour per reference kind is what makes the
listing scannable. Stick to the classes above.

### 7.4 Comment wrapping

Listing comments are rendered to HTML first, then wrapped on
visible width so `[label](address:HEX)` atoms stay intact across
the wrap step. If you write a very long link label, the wrapper
will split it into several `<a>` tags sharing the same target;
short labels stay on one line.

### 7.5 Per-version memory-map pages

Each version whose JSON contains memory-map entries gets its own
`{version_id}-memory-map.html` page. The project-index table has
a **Memory Map** column that lights up for rows with a populated
memory map. Versions without enriched labels silently skip the
column – no warning, no empty page.

---

## 8. CLI tools in each disassembly repo

The sibling repos expose a `{slug}-disasm-tool` executable via
`pyproject.toml`'s `[project.scripts]`. Sub-commands (vary by
project but consistent in spirit):

- `disassemble VERSION` – run the py8dis driver and write `.asm`
  + `.json` into `versions/{slug}-{VERSION}/output/`.
- `verify VERSION` – assemble the generated `.asm` with beebasm
  and byte-compare against the original ROM. Primary correctness
  check; must pass before any PR.
- `lint VERSION` – validate that every `comment()` /
  `subroutine()` / `label()` address in the driver corresponds to
  a real py8dis item, and that `address_links` / `glossary_links`
  in `rom.json` and `acornaeology.json` resolve.
- `insert-point VERSION ADDRESS` – show where a new
  `subroutine(0xADDRESS, …)` declaration should go in the driver
  to keep address-ordered sections coherent.
- `rename-labels VERSION` – apply a batch of label renames
  consistently across the driver and any writeup references.
- `compare / audit / cfg / context` – analysis helpers (see
  `--help`).

### 8.1 Ship checklist

Before closing a feature PR:

```sh
uv run {slug}-disasm-tool disassemble VERSION
uv run {slug}-disasm-tool verify VERSION
uv run {slug}-disasm-tool lint VERSION
(cd …/acornaeology.github.io && uv run python -m generator.build)
```

- Round-trip `verify` must pass (byte-identical).
- `lint` must pass (no broken references).
- The site must build with **zero warnings**. `address:…` warnings
  are authoring bugs – fix them, don't ignore them.
- If you enriched labels, open the rendered memory-map page and
  spot-check: groups ordered, tooltips readable, descriptions
  render tables / lists correctly.

---

## 9. Migrating a legacy driver

If your project's driver pre-dates these capabilities, here's a
sensible migration order. Each step is independently shippable.

1. **Comment Markdown.** Sweep inline comments for hex references
   (`&XXXX`) and convert to `[label](address:HEX)` or
   `[label](address:HEX?hex)`. Drop any leftover "see &XXXX"
   prose once the link is explicit.
2. **Subroutine titles + descriptions.** Go routine by routine;
   give each a one-sentence `title` and at least a short
   `description` paragraph. Use tables where there's a mapping
   (registers in / registers out, ctrl-byte dispatch, etc.).
3. **Memory map metadata.** For each non-ROM label, add
   `description=` / `length=` / `group=` / `access=`. Use the
   `\n`-in-first-paragraph convention for tooltip-length briefs.
4. **ROM-tail banners.** Mark gap regions, padding, and the
   hardware vector table with `subroutine()`-style banners so the
   listing breaks cleanly at each landmark.
5. **Typography pass.** Run a script that replaces ` -> ` /
   ` <- ` / ` -- ` (including existing `—`) / `...` with their
   Unicode equivalents. Leave operator tokens alone.
6. **Analysis writeups.** Re-read each analysis for
   internal-monologue leaks and external-source voice; migrate
   any surviving bare-hex references to `[label](address:HEX)`.
7. **Site build.** Rebuild, fix warnings, spot-check the
   generated pages.

Keep commits small and thematic. `verify` after every step.

---

## 10. Pointers to reference material

- **Worked example.** `acorn-econet-bridge` — driver, analyses,
  memory map all adopt these conventions.
- **py8dis docs.** `py8dis/README.md` plus the tests in
  `py8dis/tests/`. Memory-map-specific tests live in
  `py8dis/tests/test_memory_map.py`.
- **Site generator.** `acornaeology.github.io/generator/` —
  `build.py`, `disassembly.py`, `markdown_listing.py`,
  `glossary.py`. Start with `build.py` for the orchestration and
  follow the renderer pipeline from there.
- **Repo-local conventions.** Each disassembly repo has its own
  `CLAUDE.md` at the root with project-specific hardware notes
  and CLI commands. Read it before starting work in that repo.
