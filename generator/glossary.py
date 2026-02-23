"""Parses glossary markdown and applies glossary links to HTML content."""

import re

from markupsafe import escape

_TERM_RE = re.compile(r"^\*\*(.+?)\*\*(?:\s*\((.+?)\))?\s*$")


def _slugify(text):
    """Convert text to a URL-friendly slug."""
    slug = text.lower().replace(" ", "-")
    return re.sub(r"[^a-z0-9-]", "", slug)


def parse_glossary(md_text):
    """Parse GLOSSARY.md into structured data.

    The glossary uses the Pandoc multi-paragraph definition list convention
    to encode brief and extended descriptions:

        **TERM** (Expansion)
        : Brief definition — one or two sentences. What the term IS.

          Extended detail — how NFS uses it, implementation specifics,
          or additional context. Shown only on the glossary page.

    First paragraph (the `: ` line and its continuations before any blank
    line) = brief, used for tooltip text in doc pages. Subsequent indented
    paragraphs after a blank line = extended, shown only on the glossary
    page. Entries without extended detail keep a single paragraph.

    Returns a dict with:
        preamble   - markdown text before the first category heading
        categories - list of category dicts, each with:
            name  - category heading text
            slug  - URL-friendly slug
            terms - list of term dicts, each with:
                term       - the term key (e.g. "BRKV")
                slug       - URL-friendly slug
                expansion  - optional expansion text (e.g. "Break Vector")
                brief      - first paragraph (for tooltips)
                extended   - additional indented paragraphs (glossary page)
    """
    lines = md_text.split("\n")

    preamble_lines = []
    categories = []
    current_category = None
    current_term = None

    for line in lines:
        # Skip the H1 heading
        if line.startswith("# ") and not line.startswith("## "):
            continue

        # Category heading
        if line.startswith("## "):
            _finish_term(current_term, current_category)
            current_term = None
            current_category = {
                "name": line[3:].strip(),
                "slug": _slugify(line[3:].strip()),
                "terms": [],
            }
            categories.append(current_category)
            continue

        # Before any category — collect preamble
        if current_category is None:
            preamble_lines.append(line)
            continue

        # Term heading
        m = _TERM_RE.match(line)
        if m:
            _finish_term(current_term, current_category)
            current_term = {
                "term": m.group(1),
                "slug": _slugify(m.group(1)),
                "expansion": m.group(2),
                "brief_lines": [],
                "extended_lines": [],
                "in_extended": False,
            }
            continue

        # Lines within a term entry
        if current_term is not None:
            if line.startswith(": "):
                current_term["brief_lines"].append(line[2:])
            elif line.strip() == "":
                # Blank line transitions from brief to extended
                if current_term["brief_lines"]:
                    current_term["in_extended"] = True
                if current_term["in_extended"]:
                    current_term["extended_lines"].append("")
            elif current_term["in_extended"]:
                # Extended detail (indented paragraphs)
                current_term["extended_lines"].append(line)
            elif current_term["brief_lines"]:
                # Continuation of the brief paragraph
                current_term["brief_lines"].append(line.strip())

    _finish_term(current_term, current_category)

    # Clean up preamble
    preamble = "\n".join(preamble_lines).strip()

    return {"preamble": preamble, "categories": categories}


def _finish_term(term, category):
    """Finalise a term entry and add it to the category."""
    if term is None or category is None:
        return

    brief = " ".join(term["brief_lines"])

    # Clean up extended: strip leading/trailing blank lines, preserve
    # internal structure
    ext_lines = term["extended_lines"]
    while ext_lines and ext_lines[0].strip() == "":
        ext_lines.pop(0)
    while ext_lines and ext_lines[-1].strip() == "":
        ext_lines.pop()
    extended = "\n".join(ext_lines) if ext_lines else ""

    category["terms"].append({
        "term": term["term"],
        "slug": term["slug"],
        "expansion": term["expansion"],
        "brief": brief,
        "extended": extended,
    })


def build_glossary_lookup(glossary):
    """Build a flat lookup dict keyed by term name.

    Returns:
        {"BRKV": {"slug": "brkv", "expansion": "Break Vector",
                  "brief": "MOS vector at ...", ...}, ...}
    """
    lookup = {}
    for category in glossary["categories"]:
        for entry in category["terms"]:
            lookup[entry["term"]] = {
                "slug": entry["slug"],
                "expansion": entry["expansion"],
                "brief": entry["brief"],
            }
    return lookup


def _build_tooltip(entry):
    """Build tooltip text from a glossary lookup entry."""
    if entry["expansion"]:
        return f"{entry['expansion']}: {entry['brief']}"
    return entry["brief"]


def _find_text_occurrences(html_text, pattern):
    """Find all occurrences of pattern in HTML text nodes.

    Returns a list of (start, end) tuples for matches that fall outside
    HTML tags and outside <a>...</a> elements."""
    matches = []
    i = 0
    inside_tag = False
    tag_start = 0
    anchor_depth = 0
    pat_len = len(pattern)

    while i < len(html_text):
        ch = html_text[i]

        if ch == "<":
            inside_tag = True
            tag_start = i
            i += 1
            continue

        if ch == ">" and inside_tag:
            tag_content = html_text[tag_start + 1:i]
            tag_lower = tag_content.lower().lstrip()
            if tag_lower.startswith("a ") or tag_lower == "a":
                anchor_depth += 1
            elif tag_lower.startswith("/a"):
                anchor_depth = max(0, anchor_depth - 1)
            inside_tag = False
            i += 1
            continue

        if not inside_tag and anchor_depth == 0:
            if html_text[i:i + pat_len] == pattern:
                matches.append((i, i + pat_len))
                i += pat_len
                continue

        i += 1

    return matches


def apply_glossary_links(html_text, glossary_links, glossary_lookup, slug):
    """Apply glossary links to HTML content.

    Wraps matched terms with <a> elements linking to the glossary page.
    Matches only in text nodes, skipping content inside HTML tags and
    existing <a> elements.

    Args:
        html_text:       HTML string (post markdown conversion)
        glossary_links:  list of dicts with pattern, occurrence, term
        glossary_lookup: dict from build_glossary_lookup()
        slug:            source slug for constructing glossary URL
    """
    replacements = []

    for link_spec in glossary_links:
        pattern = link_spec["pattern"]
        occurrence = link_spec["occurrence"]
        term = link_spec["term"]

        entry = glossary_lookup.get(term)
        if not entry:
            print(f"  Warning: glossary term '{term}' not found in glossary")
            continue

        matches = _find_text_occurrences(html_text, pattern)
        if not matches:
            print(f"  Warning: glossary pattern '{pattern}' not found in HTML")
            continue

        idx = occurrence if occurrence >= 0 else len(matches) + occurrence
        if idx < 0 or idx >= len(matches):
            print(f"  Warning: occurrence {occurrence} out of range "
                  f"for glossary pattern '{pattern}'")
            continue

        start, end = matches[idx]
        tooltip = _build_tooltip(entry)
        matched_text = html_text[start:end]
        replacement = (
            f'<a href="glossary.html#term-{entry["slug"]}"'
            f' class="glossary-ref"'
            f' data-tip="{escape(tooltip)}">{escape(matched_text)}</a>'
        )
        replacements.append((start, end, replacement))

    # Apply end-to-start to avoid position shifts
    replacements.sort(key=lambda r: r[0], reverse=True)
    for start, end, replacement in replacements:
        html_text = html_text[:start] + replacement + html_text[end:]

    return html_text
