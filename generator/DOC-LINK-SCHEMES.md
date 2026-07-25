# Documentation link schemes

Inline URL schemes authors use in Markdown docs (per-version and
project-level analyses). Each is resolved by a post-Markdown pass in
`generator/build.py` into a final `<a>`. All are **explicit**: the author
writes the link where they want it, so there is no text scanning, no
occurrence counting, and no mid-word ambiguity.

All three work in **both** contexts, exactly as `address:` already does:

- **Markdown docs** (per-version and project-level analyses) — resolved
  by `build.py`.
- **In-code comments and subroutine descriptions** (the `d.comment()` /
  `d.subroutine(description=…)` text rendered onto the disassembly
  listing) — resolved by `markdown_listing.py`.

Both resolvers must be added in each new scheme. For comments this is the
whole point of `label:`: a comment that references another routine by
`label:NAME` keeps pointing at it after code shifts between versions,
instead of the numeric `address:HEX` going stale (the failure that forced
~494 manual remaps in the ANFS 4.24 work).

There are three schemes:

| Scheme | Targets | Stable across versions? |
|---|---|---|
| `address:HEX[@version][?hex]` | a numeric ROM address | no — the address is literal |
| `label:NAME[@version][?hex]` | a symbolic disassembly label | **yes** — resolves to the label's current address at build time |
| `glossary:TERM` | a glossary entry | n/a |

`address:` is unchanged (see `apply_address_uri_links`). `label:` and
`glossary:` are the additions that retire the old fuzzy `glossary_links`
matcher.

## `glossary:SLUG`

    …the [CMOS](glossary:cmos) clock…
    …read through the [MOS](glossary:mos) vector…
    …on the [Master 128](glossary:master-128)…

- `SLUG` is the glossary anchor slug — the URL-safe form of the term
  (`cmos`, `mos`, `master-128`), the same slug used in the
  `glossary.html#term-{slug}` anchor. Matched **case-insensitively**, so
  `glossary:CMOS` and `glossary:cmos` are equivalent. A Markdown link
  destination cannot contain spaces, so multi-word terms are always
  referenced by their hyphenated slug.
- Markdown converts the link to `<a href="glossary:TERM">text</a>`;
  `apply_glossary_uri_links()` rewrites it to
  `<a href="glossary.html#term-{slug}" class="glossary-ref"
  data-tip="{tooltip}">text</a>` (identical output to the old matcher,
  just author-placed).
- Unknown `TERM` → warning, tag left in place.

**Why this replaces `glossary_links`.** The old mechanism scanned each
doc for the Nth raw substring of a pattern. A short term that is also a
substring of a longer word linked mid-word (`MOS` inside `CMOS`, `NFS`
inside `ANFS`), and every `occurrence` index silently drifted whenever
prose was edited. The author now marks the exact word, so the whole class
of bugs disappears.

## `label:NAME[@version][?hex]`

    [rx_frame_b](label:rx_frame_b)              — label-only link
    [rx_frame_b](label:rx_frame_b?hex)          — append " (&E263)"
    [tube_page6_start](label:tube_page6_start@3.60?hex)

- `NAME` is a disassembly label name from the target version's JSON label
  set.
- Resolution: `apply_label_uri_links()` maps `NAME` → address via a
  per-version `{name: addr}` map (built from the JSON, the same source as
  `version_anchors` / `version_mm_links`), then delegates to the **exact**
  address resolution `address:` uses (memory-map page if the address is a
  mapped entry, otherwise the ROM-range anchor). The `?hex` flag appends
  ` (&XXXX)` using the *resolved* address.
- `@version` optional; defaults to the doc's own version (project-level
  analyses must qualify).
- Unknown `NAME` in that version → warning, tag left in place.

**Why a separate scheme, not overloaded `address:`.** A label can be
named like a hex address (`E263`), so a single scheme can't tell a
symbolic label from a literal address — the prefix disambiguates. More
importantly, `label:` is **stable**: it resolves to wherever the label
lives in each version, so a carried-over description never goes stale when
code shifts between versions (the failure mode that required ~494 manual
link remaps in the ANFS 4.24 work).

## Lint (fantasm)

- `glossary:TERM` — `TERM` must exist in `GLOSSARY.md`.
- `label:NAME[@version]` — `NAME` must exist in the (resolved version's)
  JSON label set.
- `address:HEX[@version]` — unchanged.

Once every project has migrated, the `glossary_links` occurrence
validation (`find_nth_occurrence`) is removed.

## Migration & retirement

1. Add `glossary:` + `label:` resolution to **both** `build.py` (docs) and
   `markdown_listing.py` (in-code comments), alongside the old matcher so
   the site keeps building.
2. Migrate every project:
   - docs: rewrite each `glossary_links` entry to an inline
     `[text](glossary:term)` at the **word-boundary** occurrence it was
     meant for; delete the `glossary_links` arrays from `rom.json`;
   - docs + in-code comments: convert `address:HEX` links whose visible
     text is a label name to `label:NAME` (the stale-link fix — see
     `plan-symbolic-doc-links`), leaving genuine numeric-address and raw
     `&XXXX` prose references on `address:`.
3. Delete `apply_glossary_links` + `_find_text_occurrences` and the
   `glossary_links` schema/lint. Document the schemes in `AUTHORING.md`.
