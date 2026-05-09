"""Processes structured disassembly JSON into template-ready data."""

import bisect
import html as html_mod
import re

from markupsafe import Markup, escape

from .markdown_listing import render_markdown

CONTENT_MAX_WIDTH = 64
RELOCATED_MAX_WIDTH = 58

_PREFIX_WIDTH = 9   # visible width of "    EQUB " / "    EQUW "
_SEP_WIDTH = 2      # ", "
_COMMENT_GAP = 4    # "  ; "
_OUTLIER_GAP = 16   # min gap in sorted widths to split alignment groups


def _optimal_data_max_width(n_values, comment, value_width,
                            max_width=CONTENT_MAX_WIDTH):
    """Find the optimal max_width for _group_values to balance data and
    comment wrapping.

    Returns a (possibly narrower) max_width for _group_values, or the
    original max_width if the comment fits at the end of standard packing.
    """
    if not comment:
        return max_width

    step = value_width + _SEP_WIDTH

    def vpr_at(mw):
        usable = mw - _PREFIX_WIDTH
        return max(1, 1 + (usable - value_width) // step) if usable >= value_width else 1

    def row_width(vpr):
        return _PREFIX_WIDTH + vpr * value_width + max(0, vpr - 1) * _SEP_WIDTH

    # Standard packing — does the comment fit on the last line?
    std_vpr = vpr_at(max_width)
    last_count = n_values % std_vpr or std_vpr
    trailing = row_width(last_count)
    if max_width - trailing - _COMMENT_GAP >= len(comment):
        return max_width

    # Search over values-per-row for balanced layout
    def wrap_lines(text, width):
        if width < 1:
            return len(text)
        lines, col = 1, 0
        for word in text.split():
            need = (1 if col else 0) + len(word)
            if col and col + need > width:
                lines += 1
                col = len(word)
            else:
                col += need
        return lines

    best_vpr = std_vpr
    best_score = (float('inf'), float('inf'))

    for vpr in range(1, std_vpr + 1):
        widest = row_width(vpr) + (1 if n_values > vpr else 0)
        cw = max_width - widest - _COMMENT_GAP
        if cw < 1:
            continue
        d = -(-n_values // vpr)  # ceil division
        c = wrap_lines(comment, cw)
        score = (max(d, c), abs(d - c))
        if score < best_score:
            best_score = score
            best_vpr = vpr

    return row_width(best_vpr)


def process_disassembly(data, version_id=None):
    """Process structured JSON into sections of template-ready lines.

    `version_id` is the per-version URL stem (e.g. `"1"` for
    `1.html` and `1-memory-map.html`). Supply it so references to
    non-ROM labels that appear in `data["memory_map"]` become links
    to the memory-map page, using a named `target="memory-map"`
    window so repeat clicks across a session share a single
    side-by-side memory-map window.

    Returns a list of section dicts, each with:
        lines           - list of line dicts
        has_binary_addr - True if any line has a ROM source address

    Each line dict has:
        id   - HTML id attribute (str or None)
        addr - address display string (str or None)
        html - pre-rendered content (Markup)
        banner - True if this is a full-width subroutine header (optional)
    """
    # Subroutines (real entry points) and banners (data-region
    # headers, py8dis's data_banner equivalent) are kept in separate
    # arrays in the dasmos JSON schema so consumers can tell the two
    # apart. For listing rendering we treat them uniformly: both
    # produce the same structured `<div class="sub-header">` block
    # above the labelled item. Subroutines win on collisions (a real
    # subroutine and a banner shouldn't share an address, but if they
    # do the entry-point semantics carry the fall-through info that a
    # banner lacks).
    sub_lookup = {}
    for entry in data.get("banners", []):
        key = (entry["addr"], entry.get("binary_addr"))
        sub_lookup[key] = entry
    for sub in data.get("subroutines", []):
        key = (sub["addr"], sub.get("binary_addr"))
        sub_lookup[key] = sub

    item_by_addr = {item["addr"]: item for item in data["items"]}
    valid_addrs = set(item_by_addr)
    sorted_addrs = sorted(valid_addrs)

    # Build two companion lookups for label references in the listing:
    #
    # - `label_tooltips: {addr: "&XXXX - text"}` drives the `data-tip`
    #   on label references (operand labels and `[label](address:HEX)`
    #   comment links). The text comes from the memory-map `brief` for
    #   non-ROM labels and from the subroutine `title` for ROM labels,
    #   so hovering `STA adlc_a_cr1` shows "&C800 - ADLC A control
    #   /status port 0." and hovering `JSR rx_frame_a` shows "&E0E2 -
    #   RX frame handler, side A". Addresses without a memory-map
    #   brief or subroutine title fall back to the bare "&XXXX" form.
    #
    # - `mm_links: {addr: href}` turns memory-map references into real
    #   `<a>` elements pointing at the memory-map page, with
    #   `target="memory-map"` so a side-by-side memory-map window is
    #   reused across clicks.
    #
    # Py8dis' `brief` is Markdown-stripped per the author-controlled
    # `\n`-in-first-paragraph convention; subroutine titles are plain
    # text by convention.
    label_tooltips = {}
    mm_links = {}
    for entry in data.get("memory_map", []):
        addr = entry["addr"]
        brief = entry.get("brief")
        if brief:
            label_tooltips[addr] = f"&{addr:04X} \u2013 {brief}"
        if version_id is not None:
            mm_links[addr] = f"{version_id}-memory-map.html#mm-{entry['name']}"
    for sub in data.get("subroutines", []):
        title = sub.get("title")
        if title:
            addr = sub["addr"]
            label_tooltips[addr] = f"&{addr:04X} \u2013 {title}"
    for banner in data.get("banners", []):
        title = banner.get("title")
        if title:
            addr = banner["addr"]
            label_tooltips.setdefault(addr, f"&{addr:04X} \u2013 {title}")

    # Pre-scan to find which subroutine sections contain relocated code
    relocated_sections = _find_relocated_sections(data["items"], sub_lookup)

    lines = []
    current_sub = None
    in_relocated = False
    for item in data["items"]:
        key = (item["addr"], item.get("binary_addr"))
        sub = sub_lookup.get(key)
        if sub:
            if current_sub and current_sub.get("fall_through"):
                lines.append({
                    "id": None,
                    "addr": None,
                    "html": Markup(
                        '<span class="fall-through">'
                        'fall through \u2193</span>'
                    ),
                })
            current_sub = sub
        if sub and sub.get("title"):
            in_relocated = (item["addr"], item.get("binary_addr")) in relocated_sections
        max_width = RELOCATED_MAX_WIDTH if in_relocated else CONTENT_MAX_WIDTH
        lines.extend(_process_item(item, sub_lookup, item_by_addr, valid_addrs,
                                   sorted_addrs, label_tooltips, mm_links,
                                   max_width))

    if current_sub and current_sub.get("fall_through"):
        lines.append({
            "id": None,
            "addr": None,
            "html": Markup(
                '<span class="fall-through">'
                'fall through \u2193</span>'
            ),
        })

    _align_inline_comments(lines, valid_addrs, sorted_addrs, label_tooltips, mm_links)
    return _split_into_sections(lines)


def _process_item(item, sub_lookup, item_by_addr, valid_addrs, sorted_addrs,
                  label_tooltips, mm_links, max_width=CONTENT_MAX_WIDTH):
    lines = []
    addr = item["addr"]
    addr_id = f"addr-{addr:04X}"
    addr_display = f"{addr:04X}"
    id_used = False
    addr_shown = False

    # ROM source address for relocated code
    binary_addr_raw = item.get("binary_addr")
    if binary_addr_raw is not None:
        binary_addr_id = f"addr-{binary_addr_raw:04X}"
        binary_addr_display = f"{binary_addr_raw:04X}"
    else:
        binary_addr_id = None
        binary_addr_display = None

    sub = sub_lookup.get((addr, item.get("binary_addr")))

    # dasmos 1.5 (acornaeology/dasmos#16) splits the old comments_before /
    # comments_after fields into four per-align fields so the renderer
    # can place each comment at its authored position. Older sources
    # still emit the conflated fields; for those we route the legacy
    # data to BEFORE_LABEL / AFTER_LINE buckets respectively, matching
    # the historical default placement.
    cmt_before_label = _filter_comments(
        item.get("comments_before_label",
                 item.get("comments_before", [])), sub)
    cmt_after_label = _filter_comments(
        item.get("comments_after_label", []), sub)
    cmt_before_line = _filter_comments(
        item.get("comments_before_line", []), sub)
    cmt_after_line = _filter_comments(
        item.get("comments_after_line",
                 item.get("comments_after", [])), sub)

    # Split out ATX heading comments (`# title`, `## title`, ...) from
    # each bucket. They render as banner-style heading rows that span
    # the listing width; without this they'd pass through the inline
    # comment renderer which silently mangles the resulting <hN>.
    h_before_label, cmt_before_label = _split_atx_headings(cmt_before_label)
    h_after_label, cmt_after_label = _split_atx_headings(cmt_after_label)
    h_before_line, cmt_before_line = _split_atx_headings(cmt_before_line)
    h_after_line, cmt_after_line = _split_atx_headings(cmt_after_line)

    # Banner alignment from dasmos 1.5; older sources omit the field
    # and default to BEFORE_LABEL (matches the pre-1.5 hard-coded path).
    has_banner = bool(sub and sub.get("title"))
    banner_align = (sub or {}).get("align", "before_label") if has_banner else None

    decorated_before_label = bool(
        h_before_label or cmt_before_label
        or (has_banner and banner_align == "before_label"))
    decorated_after_label = bool(
        (has_banner and banner_align in ("after_label", "before_line"))
        or h_after_label or cmt_after_label
        or h_before_line or cmt_before_line)

    if decorated_before_label:
        lines.append(_empty_line())

    # --- BEFORE_LABEL: ATX headings, then banner card, then prose ---
    for level, text in h_before_label:
        lines.append({
            "id": addr_id if not id_used else None,
            "addr": None,
            "html": _render_heading_card(level, text, valid_addrs,
                                         sorted_addrs, label_tooltips, mm_links),
            "banner": True,
            "section_break": True,
        })
        id_used = True

    if has_banner and banner_align == "before_label":
        lines.append({
            "id": addr_id if not id_used else None,
            "addr": None,
            "html": _render_subroutine_header(sub, valid_addrs, sorted_addrs,
                                              label_tooltips, mm_links),
            "banner": True,
            "section_break": True,
        })
        id_used = True

    for comment_text in cmt_before_label:
        _append_comment_lines(lines, comment_text, max_width, valid_addrs,
                              sorted_addrs, label_tooltips, mm_links)

    # --- LABELS ---
    references = item.get("references", [])
    for label_name in item.get("labels", []):
        ref_html = _render_ref_popup(references, item_by_addr) if references else ""
        label_html = Markup(
            f'<span class="label">.{escape(label_name)}{ref_html}</span>'
        )
        lines.append({
            "id": addr_id if not id_used else None,
            "addr": addr_display if not addr_shown else None,
            "addr_id": addr_id,
            "binary_addr": binary_addr_display if not addr_shown else None,
            "binary_addr_id": binary_addr_id,
            "html": label_html,
        })
        id_used = True
        addr_shown = True

    # --- AFTER_LABEL / BEFORE_LINE: between labels and the data row ---
    # AFTER_LABEL and BEFORE_LINE are equivalent positions in the
    # current renderer (we don't emit sub-label assignment rows yet,
    # so there's nothing between them to differentiate). Banner cards
    # in this position get banner=True but NOT section_break=True --
    # otherwise the labels above would land in the previous section.
    if decorated_after_label:
        if has_banner and banner_align == "after_label":
            lines.append({
                "id": None,
                "addr": None,
                "html": _render_subroutine_header(sub, valid_addrs, sorted_addrs,
                                                  label_tooltips, mm_links),
                "banner": True,
            })

        for level, text in h_after_label:
            lines.append({
                "id": None,
                "addr": None,
                "html": _render_heading_card(level, text, valid_addrs,
                                             sorted_addrs, label_tooltips, mm_links),
                "banner": True,
            })
        for comment_text in cmt_after_label:
            _append_comment_lines(lines, comment_text, max_width, valid_addrs,
                                  sorted_addrs, label_tooltips, mm_links)

        for level, text in h_before_line:
            lines.append({
                "id": None,
                "addr": None,
                "html": _render_heading_card(level, text, valid_addrs,
                                             sorted_addrs, label_tooltips, mm_links),
                "banner": True,
            })
        for comment_text in cmt_before_line:
            _append_comment_lines(lines, comment_text, max_width, valid_addrs,
                                  sorted_addrs, label_tooltips, mm_links)

        if has_banner and banner_align == "before_line":
            lines.append({
                "id": None,
                "addr": None,
                "html": _render_subroutine_header(sub, valid_addrs, sorted_addrs,
                                                  label_tooltips, mm_links),
                "banner": True,
            })

    # --- DATA row ---
    # For byte/word items with an inline comment, compute a narrower
    # data width so both data rows and comment rows wrap over a
    # balanced number of lines rather than squeezing the comment into
    # a tiny column.
    inline_comment = item.get("comment_inline")
    render_width = max_width
    if inline_comment and item["type"] in ("byte", "word"):
        vw = _hinted_value_width(item)
        render_width = _optimal_data_max_width(
            len(item.get("values", [])), inline_comment, vw, max_width)
    content_html = _render_content(item, valid_addrs, label_tooltips, mm_links,
                                    render_width)

    line_dict = {
        "id": addr_id if not id_used else None,
        "addr": addr_display if not addr_shown else None,
        "addr_id": addr_id,
        "binary_addr": binary_addr_display if not addr_shown else None,
        "binary_addr_id": binary_addr_id,
        "html": content_html,
        "_inline_comment": inline_comment,
        "_max_width": max_width,
    }
    # For multi-line data with balanced layout, store the intended comment
    # alignment width so _align_inline_comments uses the widest data line.
    # For trailing layout (render_width unchanged), it uses the last line.
    if render_width < max_width:
        line_dict["_balanced"] = True
    lines.append(line_dict)
    id_used = True
    addr_shown = True

    # --- AFTER_LINE: below the data row ---
    for level, text in h_after_line:
        lines.append({
            "id": None,
            "addr": None,
            "html": _render_heading_card(level, text, valid_addrs,
                                         sorted_addrs, label_tooltips, mm_links),
            "banner": True,
        })
    for comment_text in cmt_after_line:
        _append_comment_lines(lines, comment_text, max_width, valid_addrs,
                              sorted_addrs, label_tooltips, mm_links)
    if has_banner and banner_align == "after_line":
        lines.append({
            "id": None,
            "addr": None,
            "html": _render_subroutine_header(sub, valid_addrs, sorted_addrs,
                                              label_tooltips, mm_links),
            "banner": True,
        })

    return lines


def _visible_width(markup):
    """Compute the visible character width of an HTML string.

    For multi-line content (e.g. grouped EQUB values), returns the width
    of the longest individual line."""
    text = re.sub(r"<[^>]+>", "", str(markup))
    text = html_mod.unescape(text)
    return max(len(line) for line in text.split("\n"))


def _trailing_line_width(markup):
    """Width of the last visible line of an HTML string.

    For single-line content this equals _visible_width.  For multi-line
    content (grouped EQUB/EQUW) the inline comment is appended after the
    last line, so this is the relevant width for comment placement."""
    text = re.sub(r"<[^>]+>", "", str(markup))
    text = html_mod.unescape(text)
    return len(text.split("\n")[-1])


def _find_break_position(word, budget):
    """Find the best position to break a long word at or before budget.

    Prefers breaking near internal punctuation (|, _, /, -), then at
    character class transitions (letter/digit/punctuation boundaries),
    and falls back to the exact budget position."""
    if budget <= 0:
        budget = 1

    # Preferred: break after punctuation characters
    best = -1
    for i in range(min(budget, len(word)) - 1, 0, -1):
        if word[i] in "|_/-":
            best = i + 1
            break
    if best > 0:
        return best

    # Second: break at character class transitions
    for i in range(min(budget, len(word)) - 1, 0, -1):
        a, b = word[i - 1], word[i]
        if (a.isalpha() != b.isalpha()) or (a.isdigit() != b.isdigit()):
            return i
    if best > 0:
        return best

    # Last resort: break at exact boundary
    return min(budget, len(word))


# HTML-level tokeniser for comment-text wrapping. We wrap AFTER
# rendering to HTML so we can measure on visible width rather than
# Markdown-source width -- a `[ram_test_fail](address:F28C)` atom
# renders to 13 visible chars, not 30. The tokeniser recognises
# three kinds of atom: `<a>` and `<code>` spans (treated as
# indivisible units up to the split step), whitespace runs, and
# plain words.
_HTML_ATOM_RE = re.compile(
    r'(?P<tag><a\s[^>]*>.*?</a>|<code>[^<]*</code>)'
    r'|(?P<ws>\s+)'
    r'|(?P<word>[^<\s]+)',
    re.DOTALL,
)


def _atom_visible_width(html):
    """Visible width of an HTML atom (tag stripped, entities decoded)."""
    text = re.sub(r'<[^>]+>', '', html)
    return len(html_mod.unescape(text))


def _split_tag_atom(html, budget):
    """Split an `<a attrs>inner</a>` or `<code>inner</code>` atom into
    multiple copies each fitting within `budget` visible chars.

    Honours a common nested case: `<a ...><code>label</code></a>`
    rewraps as `<a ...><code>piece1</code></a><a ...><code>piece2</code></a>`
    so each split piece keeps both its link target and its code
    styling. Plain `<a>` text and plain `<code>` text follow the
    same pattern without the inner wrapper.
    """
    m_ac = re.match(r'(<a\s[^>]*>)<code>(.*)</code>(</a>)', html, re.DOTALL)
    if m_ac:
        open_a, inner, close_a = m_ac.group(1), m_ac.group(2), m_ac.group(3)
        pieces = _split_word(inner, budget)
        return [f"{open_a}<code>{p}</code>{close_a}" for p in pieces]

    m_a = re.match(r'(<a\s[^>]*>)(.*)(</a>)', html, re.DOTALL)
    if m_a:
        open_a, inner, close_a = m_a.group(1), m_a.group(2), m_a.group(3)
        pieces = _split_word(inner, budget)
        return [f"{open_a}{p}{close_a}" for p in pieces]

    m_c = re.match(r'(<code>)(.*)(</code>)', html, re.DOTALL)
    if m_c:
        open_c, inner, close_c = m_c.group(1), m_c.group(2), m_c.group(3)
        pieces = _split_word(inner, budget)
        return [f"{open_c}{p}{close_c}" for p in pieces]

    return [html]


def _split_word(word, budget):
    """Split a plain word into chunks of at most `budget` chars each."""
    pieces = []
    while len(word) > budget:
        pos = _find_break_position(word, budget)
        pieces.append(word[:pos])
        word = word[pos:]
    if word:
        pieces.append(word)
    return pieces


def _wrap_comment_html(html, first_line_budget, continuation_indent,
                      max_width=CONTENT_MAX_WIDTH):
    """Wrap already-rendered HTML comment content into column-fitted lines.

    Measures on visible width (tag-stripped, entities decoded) so a
    `<a>label</a>` span occupies `len(label)` columns rather than
    the source-HTML width. Link and inline-code spans stay intact
    where possible; when one exceeds the budget, it splits into
    multiple copies of the span each carrying a slice of the inner
    text (shared target and styling).
    """
    atoms = []
    for m in _HTML_ATOM_RE.finditer(html):
        if m.group("tag"):
            tag = m.group("tag")
            atoms.append(("tag", tag, _atom_visible_width(tag)))
        elif m.group("ws"):
            # Normalise any whitespace run to a single breakable space
            atoms.append(("ws", " ", 1))
        elif m.group("word"):
            word = m.group("word")
            atoms.append(("word", word, _atom_visible_width(word)))

    result_lines = []
    current = ""
    current_w = 0
    continuation_budget = max(1, max_width - continuation_indent)

    for kind, atom, w in atoms:
        budget = first_line_budget if not result_lines else continuation_budget

        if kind == "ws":
            # Drop leading whitespace, coalesce into a single space
            # between content atoms. If the space would overflow we
            # emit the line break instead.
            if current_w == 0:
                continue
            if current_w + 1 <= budget:
                current += " "
                current_w += 1
            else:
                result_lines.append(current.rstrip())
                current = ""
                current_w = 0
            continue

        # Non-whitespace atom
        if current_w + w <= budget:
            current += atom
            current_w += w
            continue

        # Doesn't fit on the current line: commit and retry on a fresh line
        if current:
            result_lines.append(current.rstrip())
            current = ""
            current_w = 0
        budget = continuation_budget

        if w <= budget:
            current = atom
            current_w = w
            continue

        # Atom itself exceeds the budget -- split it.
        if kind == "tag":
            pieces = _split_tag_atom(atom, budget)
        else:
            pieces = _split_word(atom, budget)
        for piece in pieces[:-1]:
            result_lines.append(piece)
        current = pieces[-1]
        current_w = _atom_visible_width(current)

    if current.strip():
        result_lines.append(current.rstrip())

    return result_lines or [""]


def _group_values(parts, prefix_width, value_width,
                  max_width=CONTENT_MAX_WIDTH, widths=None):
    """Group data values into lines that fit within max_width.

    Returns a list of lists (groups of parts per line).
    If *widths* is given, it supplies per-part visible widths instead
    of using the fixed *value_width* for every part."""
    line_groups = []
    current_group = []
    current_width = prefix_width

    for i, part in enumerate(parts):
        w = widths[i] if widths else value_width
        needed = (2 if current_group else 0) + w
        if current_width + needed > max_width and current_group:
            line_groups.append(current_group)
            current_group = [part]
            current_width = prefix_width + w
        else:
            current_group.append(part)
            current_width += needed
    if current_group:
        line_groups.append(current_group)

    return line_groups


def _split_width_outliers(items):
    """Sub-group single-line items by width, splitting at large gaps.

    Sorts the distinct widths and finds the largest gap between
    consecutive values.  If that gap exceeds _OUTLIER_GAP, items are
    split into a normal group (at or below the gap) and an outlier
    group (above it), so outliers get independent comment alignment."""
    if len(items) < 3:
        return [items]

    widths = [_visible_width(l["html"]) for _, l in items]
    sorted_w = sorted(set(widths))

    if len(sorted_w) < 2:
        return [items]

    max_gap = 0
    split_above = None
    for a, b in zip(sorted_w, sorted_w[1:]):
        gap = b - a
        if gap > max_gap:
            max_gap = gap
            split_above = a

    if max_gap < _OUTLIER_GAP:
        return [items]

    normal = [(i, l) for (i, l), w in zip(items, widths) if w <= split_above]
    outliers = [(i, l) for (i, l), w in zip(items, widths) if w > split_above]
    groups = []
    if normal:
        groups.append(normal)
    if outliers:
        groups.append(outliers)
    return groups


def _align_inline_comments(lines, valid_addrs, sorted_addrs, label_tooltips, mm_links):
    """Align inline comments within blocks separated by labels/banners.

    Single-line content (code instructions) within each block shares a
    common comment column — the position of the widest line, plus
    padding.  Multi-line content (grouped EQUB/EQUW) is excluded from
    block-wide alignment so its width doesn't push other comments off
    the edge; its comment is placed after its own last line instead."""
    blocks = _split_into_blocks(lines)

    for block in blocks:
        commented = [(i, line) for i, line in block
                     if line.get("_inline_comment")]
        if not commented:
            continue

        # Separate single-line and multi-line content for alignment
        single = [(i, l) for i, l in commented
                  if "\n" not in str(l["html"])]
        multi = [(i, l) for i, l in commented
                 if "\n" in str(l["html"])]

        # Align single-line comments to a shared column, splitting
        # width outliers into their own alignment group
        if single:
            for group in _split_width_outliers(single):
                align_w = max(_visible_width(l["html"]) for _, l in group)
                for _, line in group:
                    _merge_inline_comment(line, align_w, valid_addrs,
                                         sorted_addrs, label_tooltips, mm_links)

        # Multi-line: balanced layout aligns to widest data line;
        # trailing layout aligns to the last data line.
        for _, line in multi:
            if line.get("_balanced"):
                align_w = _visible_width(line["html"])
                _merge_inline_comment(line, align_w, valid_addrs,
                                      sorted_addrs, label_tooltips, mm_links, balanced=True)
            else:
                align_w = _trailing_line_width(line["html"])
                _merge_inline_comment(line, align_w, valid_addrs,
                                      sorted_addrs, label_tooltips, mm_links)

    # Clean up: remove internal keys from line dicts
    for line in lines:
        line.pop("_inline_comment", None)
        line.pop("_max_width", None)
        line.pop("_balanced", None)


def _merge_inline_comment(line, align_width, valid_addrs, sorted_addrs,
                          label_tooltips, mm_links, balanced=False):
    """Merge an inline comment into a line's HTML at the given alignment.

    When balanced=True (two-column EQUB layout), comment lines are
    interleaved alongside data lines from the top, producing a
    side-by-side two-column layout."""
    comment = line.pop("_inline_comment")
    line_max = line.get("_max_width", CONTENT_MAX_WIDTH)

    if balanced:
        _merge_balanced_comment(line, align_width, comment, line_max,
                                valid_addrs, sorted_addrs, label_tooltips, mm_links)
        return

    code_width = _trailing_line_width(line["html"])
    padding = " " * (align_width - code_width + 2)
    comment_col = align_width + 2 + 2  # padding + "; "

    # Render to HTML up-front so `[label](address:HEX)` link syntax
    # survives the wrap step; then measure / wrap on visible width.
    rendered = str(_linkify_comment_text(
        comment, valid_addrs, sorted_addrs, label_tooltips, mm_links))
    total_width = comment_col + _atom_visible_width(rendered)

    if total_width <= line_max:
        line["html"] = line["html"] + Markup(
            f'{padding}<span class="comment">'
            f'; {rendered}</span>'
        )
    else:
        first_budget = line_max - comment_col
        wrapped = _wrap_comment_html(rendered, first_budget, comment_col,
                                     line_max)
        indent_str = " " * comment_col
        parts = [wrapped[0]]
        for cont in wrapped[1:]:
            parts.append(f"\n{indent_str}{cont}")
        comment_html = "".join(parts)
        line["html"] = line["html"] + Markup(
            f'{padding}<span class="comment">'
            f'; {comment_html}</span>'
        )


def _merge_balanced_comment(line, align_width, comment, line_max,
                            valid_addrs, sorted_addrs, label_tooltips, mm_links):
    """Interleave comment lines alongside data lines from the top.

    Produces a two-column layout where data fills the left and the
    comment fills the right, both starting on the first line."""
    comment_w = line_max - align_width - _COMMENT_GAP
    # Render the comment to HTML once, then wrap the HTML on visible
    # width so Markdown link atoms stay intact across the wrap step.
    rendered = str(_linkify_comment_text(
        comment, valid_addrs, sorted_addrs, label_tooltips, mm_links))
    wrapped = _wrap_comment_html(rendered, comment_w, 0, comment_w)
    data_lines = str(line["html"]).split("\n")

    result = []
    for i in range(max(len(data_lines), len(wrapped))):
        if i < len(data_lines):
            data_html = data_lines[i]
            data_w = _visible_width(Markup(data_html))
        else:
            data_html = ""
            data_w = 0
        if i < len(wrapped):
            pad = " " * (align_width - data_w + 2)
            result.append(
                f'{data_html}{pad}'
                f'<span class="comment">; {wrapped[i]}</span>')
        else:
            result.append(data_html)

    line["html"] = Markup("\n".join(result))


def _split_into_blocks(lines):
    """Split lines into blocks at label boundaries, subroutine headers,
    and multi-line content (EQUB/EQUW blocks).

    Returns a list of blocks, where each block is a list of (index, line)
    tuples.  Multi-line content gets its own block so its width doesn't
    affect comment alignment of adjacent code instructions."""
    blocks = []
    current = []

    for i, line in enumerate(lines):
        is_label = '<span class="label">' in str(line.get("html", ""))
        is_banner = line.get("banner")
        is_multiline = "\n" in str(line.get("html", ""))

        if is_label or is_banner or is_multiline:
            if current:
                blocks.append(current)
            current = [(i, line)]
        else:
            current.append((i, line))

    if current:
        blocks.append(current)

    return blocks


def _split_into_sections(lines):
    """Split lines into sections at section-break banner rows.

    Each section is a dict with:
        lines           - the line dicts in this section
        has_binary_addr - whether any line has a ROM source address

    This allows each section to be rendered as a separate table, so the
    extra ROM address column only appears in relocated code sections.
    Only banners that mark a section boundary (BEFORE_LABEL banners and
    ATX-heading rows above an item's labels) carry `section_break=True`;
    AFTER_LABEL / AFTER_LINE banners stay in the same section as their
    item's label and data rows.
    """
    sections = []
    current_lines = []

    for line in lines:
        if line.get("section_break") and current_lines:
            sections.append(_make_section(current_lines))
            current_lines = []
        current_lines.append(line)

    if current_lines:
        sections.append(_make_section(current_lines))

    return sections


def _find_relocated_sections(items, sub_lookup):
    """Return the set of subroutine start addresses whose sections contain
    any relocated items (items with binary_addr)."""
    relocated = set()
    current_start = None
    has_binary = False

    for item in items:
        sub = sub_lookup.get((item["addr"], item.get("binary_addr")))
        if sub and sub.get("title"):
            if has_binary and current_start is not None:
                relocated.add(current_start)
            current_start = (item["addr"], item.get("binary_addr"))
            has_binary = False
        if "binary_addr" in item:
            has_binary = True

    if has_binary and current_start is not None:
        relocated.add(current_start)

    return relocated


def _make_section(lines):
    has_binary_addr = any(line.get("binary_addr") for line in lines)
    return {"lines": lines, "has_binary_addr": has_binary_addr}


_MD_TABLE_RE = re.compile(
    r'(?m)'
    r'^[ \t]*\|[^\n]*\|[ \t]*\n'      # header row
    r'[ \t]*\|[ \t:\-|]+\|[ \t]*\n'   # separator row
    r'(?:[ \t]*\|[^\n]*\|[ \t]*\n?)+'  # one or more body rows
)


def _split_comment_segments(text):
    """Split comment text into ('prose', text) and ('table', md) segments.

    Pipe-tables are detected as standalone segments so they can be
    rendered as monospace box-drawn tables instead of line-by-line
    prose. The surrounding text retains the per-line inline-Markdown
    rendering, which is what authors expect for paragraphs.
    """
    segments = []
    pos = 0
    for m in _MD_TABLE_RE.finditer(text):
        if m.start() > pos:
            segments.append(("prose", text[pos:m.start()]))
        segments.append(("table", m.group(0)))
        pos = m.end()
    if pos < len(text):
        segments.append(("prose", text[pos:]))
    if not segments:
        segments.append(("prose", text))
    return segments


def _append_comment_lines(lines, comment_text, max_width, valid_addrs,
                          sorted_addrs, label_tooltips, mm_links):
    """Append rendered comment rows for a multi-line Markdown comment.

    GFM pipe-tables route through `_render_md_table_text` so they
    render as Unicode box-drawn tables sized to the column budget;
    everything else keeps the existing per-line inline-Markdown path.
    """
    for kind, segment in _split_comment_segments(str(comment_text)):
        if kind == "table":
            for tline in _render_md_table_text(
                    segment, max_width - 2, valid_addrs, sorted_addrs,
                    label_tooltips, mm_links):
                lines.append({
                    "id": None, "addr": None,
                    "html": Markup(
                        f'<span class="comment">; {tline}</span>')
                })
        else:
            _append_prose_lines(lines, segment, max_width, valid_addrs,
                                sorted_addrs, label_tooltips, mm_links)


def _append_prose_lines(lines, comment_text, max_width, valid_addrs,
                        sorted_addrs, label_tooltips, mm_links):
    """Per-line inline-Markdown rendering for a prose comment segment."""
    comment_prefix_width = 2  # "; "
    for line_text in str(comment_text).split("\n"):
        if not line_text.strip():
            lines.append({"id": None, "addr": None, "html": Markup("")})
            continue

        # Skip wrapping for indented lines (preformatted content)
        if line_text.startswith("  "):
            html = Markup(
                '<span class="comment">; '
                f'{_linkify_comment_text(line_text, valid_addrs, sorted_addrs, label_tooltips, mm_links)}</span>'
            )
            lines.append({"id": None, "addr": None, "html": html})
            continue

        # Render to HTML once, then measure / wrap on visible width so
        # `[label](address:HEX)` link syntax survives the wrap step.
        rendered = str(_linkify_comment_text(
            line_text, valid_addrs, sorted_addrs, label_tooltips, mm_links))
        visible = _atom_visible_width(rendered)
        total_width = comment_prefix_width + visible
        if total_width <= max_width:
            html = Markup(
                f'<span class="comment">; {rendered}</span>'
            )
            lines.append({"id": None, "addr": None, "html": html})
        else:
            budget = max_width - comment_prefix_width
            wrapped = _wrap_comment_html(rendered, budget,
                                         comment_prefix_width, max_width)
            parts = [wrapped[0]]
            for cont in wrapped[1:]:
                parts.append(f"\n; {cont}")
            comment_html = "".join(parts)
            html = Markup(
                f'<span class="comment">; {comment_html}</span>'
            )
            lines.append({"id": None, "addr": None, "html": html})


def _render_md_table_text(table_md, max_width, valid_addrs, sorted_addrs,
                          label_tooltips, mm_links):
    """Render a GFM pipe-table as monospace box-drawn lines.

    Returns a list of one-line strings whose visible width fits within
    `max_width` (the cell budget after the leading `; `). Cell content
    runs through the inline-Markdown renderer first, then through
    `_wrap_comment_html` so wrapped HTML atoms (links, `<code>`) keep
    their target and styling across line breaks. Column widths are
    natural where they fit, falling back to longest-atom minimums plus
    a deficit-greedy expansion when the row would exceed the budget.
    """
    md_lines = [l for l in table_md.split("\n") if l.strip()]
    if len(md_lines) < 2:
        return []

    placeholder = "\x00"

    def split_row(line):
        line = line.strip()
        if line.startswith("|"):
            line = line[1:]
        if line.endswith("|"):
            line = line[:-1]
        line = line.replace(r"\|", placeholder)
        return [c.strip().replace(placeholder, "|") for c in line.split("|")]

    header_md = split_row(md_lines[0])
    body_md = [split_row(l) for l in md_lines[2:]]
    n_cols = len(header_md)
    if n_cols == 0:
        return []
    body_md = [(r + [""] * n_cols)[:n_cols] for r in body_md]

    def render_cell(text):
        return str(_linkify_comment_text(
            text, valid_addrs, sorted_addrs, label_tooltips, mm_links))

    header_html = [render_cell(c) for c in header_md]
    body_html = [[render_cell(c) for c in row] for row in body_md]

    def longest_atom_width(html):
        widths = [1]
        for m in _HTML_ATOM_RE.finditer(html):
            if m.group("tag"):
                widths.append(_atom_visible_width(m.group("tag")))
            elif m.group("word"):
                widths.append(_atom_visible_width(m.group("word")))
        return max(widths)

    natural = []
    minw = []
    for col in range(n_cols):
        cells = [header_html[col]] + [row[col] for row in body_html]
        natural.append(max((_atom_visible_width(c) for c in cells), default=1))
        minw.append(max((longest_atom_width(c) for c in cells), default=1))

    # Border overhead per row: (n_cols + 1) vertical bars + 2 spaces
    # of cell padding on each side of every column.
    overhead = (n_cols + 1) + (2 * n_cols)
    avail = max_width - overhead

    if sum(natural) <= avail:
        widths = list(natural)
    else:
        widths = list(minw)
        remaining = avail - sum(widths)
        while remaining > 0:
            best, best_deficit = -1, 0
            for i in range(n_cols):
                if widths[i] < natural[i]:
                    deficit = natural[i] - widths[i]
                    if deficit > best_deficit:
                        best_deficit = deficit
                        best = i
            if best == -1:
                break
            widths[best] += 1
            remaining -= 1

    def wrap_cell(html, w):
        return _wrap_comment_html(html, w, 0, w) or [""]

    header_wrapped = [wrap_cell(html, widths[i])
                      for i, html in enumerate(header_html)]
    body_wrapped = [
        [wrap_cell(html, widths[i]) for i, html in enumerate(row)]
        for row in body_html
    ]

    def pad(html, w):
        return html + " " * max(0, w - _atom_visible_width(html))

    def format_row(cells_lines):
        max_h = max(len(c) for c in cells_lines)
        out_rows = []
        for i in range(max_h):
            parts = []
            for col, cls in enumerate(cells_lines):
                cell_line = cls[i] if i < len(cls) else ""
                parts.append(" " + pad(cell_line, widths[col]) + " ")
            out_rows.append("│" + "│".join(parts) + "│")
        return out_rows

    def hline(left, mid, right):
        return left + mid.join("─" * (w + 2) for w in widths) + right

    out = [hline("┌", "┬", "┐")]
    out.extend(format_row(header_wrapped))
    out.append(hline("├", "┼", "┤"))
    for row_w in body_wrapped:
        out.extend(format_row(row_w))
    out.append(hline("└", "┴", "┘"))

    return out


def _empty_line():
    return {"id": None, "addr": None, "html": Markup("")}


def _render_ref_popup(references, item_by_addr):
    """Render a come-from popup showing all callers of this label."""
    refs_sorted = sorted(references)
    count = len(refs_sorted)
    parts = [
        f'<span class="ref-badge">\u2190{count}</span>',
        '<span class="ref-popup">',
    ]
    for ref_addr in refs_sorted:
        ref_item = item_by_addr.get(ref_addr)
        if ref_item and ref_item.get("type") == "code":
            mnemonic = ref_item["mnemonic"].upper()
        else:
            mnemonic = "ref"
        parts.append(
            f'<a href="#addr-{ref_addr:04X}">'
            f'\u2190 {ref_addr:04X} {escape(mnemonic)}</a>'
        )
    parts.append('</span>')
    return Markup("".join(parts))


def _is_reference_comment(text):
    """Auto-generated cross-reference comments are redundant."""
    return text.startswith("&") and "referenced" in text


def _is_banner_line(text):
    """Lines made entirely of asterisks are banner decorations."""
    stripped = text.strip()
    return len(stripped) > 3 and all(c == "*" for c in stripped)


def _is_banner_content(text, sub):
    """Check if a comment is the body of a subroutine banner that we've
    already rendered from the structured data."""
    title = sub.get("title", "")
    if title and text.startswith(title):
        return True
    return False


def _filter_comments(raw, sub):
    """Drop reference, banner-separator, and banner-body comments."""
    out = []
    for c in raw:
        if _is_reference_comment(c) or _is_banner_line(c):
            continue
        if sub and sub.get("title") and _is_banner_content(c, sub):
            continue
        out.append(c)
    return out


_ATX_HEADING_RE = re.compile(r'^\s*(#{1,6})\s+(.+?)(?:\s+#+\s*)?$')


def _atx_heading_match(text):
    """Return (level, content) if text is a single-line ATX heading.

    Single-line means the text body is one line -- a heading buried in
    the middle of a multi-paragraph comment is left alone, since
    splitting it out would reorder content the author wrote together.
    """
    if "\n" in text.strip():
        return None
    m = _ATX_HEADING_RE.match(text)
    if not m:
        return None
    return len(m.group(1)), m.group(2).rstrip()


def _split_atx_headings(comments):
    """Partition comments into ((level, text) headings, regular comments)."""
    headings, regular = [], []
    for c in comments:
        m = _atx_heading_match(c)
        if m:
            headings.append(m)
        else:
            regular.append(c)
    return headings, regular


def _render_heading_card(level, text, valid_addrs, sorted_addrs,
                         label_tooltips, mm_links):
    """Render an ATX heading from a comment as a sub-header card row.

    The page's listing already carries an <h1>, so comment-level
    headings start at <h2>. Reuses the .sub-header wrapper so the
    visual treatment matches structured banner cards.
    """
    inner = _linkify_comment_text(
        text, valid_addrs, sorted_addrs, label_tooltips, mm_links)
    tag = f"h{min(level + 1, 6)}"
    return Markup(f'<div class="sub-header"><{tag}>{inner}</{tag}></div>')


def _render_subroutine_header(sub, valid_addrs, sorted_addrs, label_tooltips, mm_links):
    """Render a subroutine's structured data as a styled HTML block."""
    parts = []
    parts.append('<div class="sub-header">')

    # Title
    title = sub.get("title", sub.get("name", ""))
    parts.append(
        f'<h3>{_linkify_comment_text(title, valid_addrs, sorted_addrs, label_tooltips, mm_links)}</h3>')

    # Description
    desc = sub.get("description", "")
    if desc:
        parts.append(
            '<div class="sub-desc">'
            f'{_render_plaintext(desc, valid_addrs, sorted_addrs, label_tooltips, mm_links)}</div>')

    # On Entry / On Exit
    entry = sub.get("on_entry", {})
    exit_ = sub.get("on_exit", {})
    if entry or exit_:
        parts.append('<div class="sub-registers"><table>')
        if entry:
            parts.append(_render_register_rows("On Entry", entry,
                                               valid_addrs, sorted_addrs,
                                               label_tooltips, mm_links))
        if exit_:
            parts.append(_render_register_rows("On Exit", exit_,
                                               valid_addrs, sorted_addrs,
                                               label_tooltips, mm_links))
        parts.append("</table></div>")

    parts.append("</div>")
    return Markup("\n".join(parts))


def _render_plaintext(text, valid_addrs, sorted_addrs, label_tooltips, mm_links):
    """Render Markdown text as block-level HTML for subroutine descriptions
    and standalone comment blocks.

    Thin wrapper around `render_markdown(inline=False)`. Paragraphs,
    lists, tables, fenced code blocks, emphasis, and inline code all
    render as their native HTML equivalents. Explicit
    `[label](address:HEX)` references resolve through the listing
    Markdown renderer; bare `&XXXX` in prose is left as plain text.
    """
    return render_markdown(str(text), valid_addrs, sorted_addrs,
                           inline=False, mm_links=mm_links,
                           label_tooltips=label_tooltips)


def _render_register_rows(heading, regs, valid_addrs, sorted_addrs, label_tooltips, mm_links):
    """Render register rows with the heading in the first column."""
    rows = []
    for i, (reg, desc) in enumerate(regs.items()):
        if i == 0:
            th = (f'<th rowspan="{len(regs)}">'
                  f'{escape(heading)}</th>')
        else:
            th = ""
        rows.append(
            f"<tr>{th}"
            f"<td>{escape(reg.upper())}</td>"
            f"<td>{_linkify_comment_text(desc, valid_addrs, sorted_addrs, label_tooltips, mm_links)}</td></tr>"
        )
    return "\n".join(rows)


def _render_content(item, valid_addrs, label_tooltips, mm_links,
                    max_width=CONTENT_MAX_WIDTH):
    t = item["type"]
    if t == "code":
        return _render_code(item, valid_addrs, label_tooltips, mm_links)
    if t == "byte":
        return _render_bytes(item, max_width)
    if t == "word":
        return _render_words(item, max_width)
    if t == "string":
        return _render_string(item)
    if t == "fill":
        return _render_fill(item)
    return Markup("")


def _render_code(item, valid_addrs, label_tooltips, mm_links):
    mnemonic = escape(item["mnemonic"].upper())
    operand = item.get("operand", "")

    html = Markup(f'    <span class="opcode">{mnemonic}</span>')
    if operand:
        operand_html = _linkify_operand(operand, item, valid_addrs,
                                        label_tooltips, mm_links)
        if _is_immediate(operand, item):
            tooltip = _immediate_tooltip(item["bytes"][1])
            operand_html = Markup(
                f'<span class="imm" data-tip="{escape(tooltip)}">'
                f'{operand_html}</span>'
            )
        html += Markup(f' <span class="operand">{operand_html}</span>')
    return html


def _linkify_operand(operand, item, valid_addrs, label_tooltips, mm_links):
    """Wrap label references in the operand text with anchor links.

    Three cases, in priority order:

    - `target` is in `mm_links`: emit an `<a class="mm-link"
      target="memory-map">` so clicking jumps to the memory-map
      entry on the per-version memory-map page.
    - `target` is a ROM item: emit a same-page `<a href="#addr-">`.
    - Otherwise: emit a plain `<span class="ext-label">` (no link).

    `label_tooltips` supplies the `data-tip` brief for any non-ROM label
    that has a memory-map description; it replaces the bare `&XXXX`
    fallback.
    """
    if "target_label" not in item or "target" not in item:
        return escape(operand)

    target_label = item["target_label"]
    target = item["target"]
    target_addr = f"&{target:04X}"
    tip = label_tooltips.get(target, target_addr)

    escaped_operand = str(escape(operand))
    escaped_label = str(escape(target_label))

    if escaped_label in escaped_operand:
        if target in mm_links:
            replacement = (
                f'<a class="mm-link" href="{mm_links[target]}"'
                f' target="memory-map"'
                f' data-tip="{escape(tip)}">{escaped_label}</a>'
            )
        elif target in valid_addrs:
            target_id = f"addr-{target:04X}"
            replacement = (f'<a href="#{target_id}"'
                           f' data-tip="{escape(tip)}">{escaped_label}</a>')
        else:
            replacement = (f'<span class="ext-label"'
                           f' data-tip="{escape(tip)}">{escaped_label}</span>')
        return Markup(escaped_operand.replace(escaped_label, replacement, 1))

    return escape(operand)


def _linkify_comment_text(text, valid_addrs, sorted_addrs, label_tooltips, mm_links):
    """Render a comment string (inline Markdown) as HTML.

    Thin wrapper around `render_markdown(inline=True)`. The Markdown
    parser handles emphasis, inline code, backslash escapes, and our
    `[label](address:HEX[?hex])` link syntax. Bare `&XXXX` references
    no longer auto-link -- authors who want a link must use the
    Markdown syntax. Address targets resolve against `mm_links`
    (memory-map entries, rendered as cross-window links) first, then
    fall back to the ROM-range anchor set (nearest preceding if the
    exact address has no anchor).
    """
    return render_markdown(str(text), valid_addrs, sorted_addrs,
                           inline=True, mm_links=mm_links,
                           label_tooltips=label_tooltips)


def _resolve_addr(addr, valid_addrs, sorted_addrs):
    """Return the item address to link to for a given address, or None.

    If addr is an exact item boundary, returns it directly.  Otherwise
    returns the nearest preceding item address (the item that contains
    this address).  Returns None if addr is before the first item."""
    if addr in valid_addrs:
        return addr
    idx = bisect.bisect_right(sorted_addrs, addr) - 1
    if idx >= 0:
        return sorted_addrs[idx]
    return None


_CONTROL_CHARS = {
    0: "NUL", 1: "SOH", 2: "STX", 3: "ETX", 4: "EOT", 5: "ENQ",
    6: "ACK", 7: "BEL", 8: "BS", 9: "HT", 10: "LF", 11: "VT",
    12: "FF", 13: "CR", 14: "SO", 15: "SI", 16: "DLE", 17: "DC1",
    18: "DC2", 19: "DC3", 20: "DC4", 21: "NAK", 22: "SYN", 23: "ETB",
    24: "CAN", 25: "EM", 26: "SUB", 27: "ESC", 28: "FS", 29: "GS",
    30: "RS", 31: "US", 32: "SP", 127: "DEL",
}


def _is_immediate(operand, item):
    """Check if this operand is an immediate value (#)."""
    return operand.startswith("#") and len(item.get("bytes", [])) == 2


def _immediate_tooltip(value):
    """Build a multi-representation tooltip for an immediate byte value."""
    parts = [str(value), f"&{value:02X}", f"%{value:08b}"]
    if value in _CONTROL_CHARS:
        parts.append(_CONTROL_CHARS[value])
    elif 33 <= value <= 126:
        parts.append(f"'{chr(value)}'")
    return "  ".join(parts)


# dasmos 1.4 (acornaeology/dasmos#14) tags byte/word elements with a
# `format_hint` so the listing can render values in the base the author
# meant. The vocabulary is the same as beebasm's. The data-tip tooltip
# always carries decimal / hex / binary / char, regardless of the hint,
# so any alternate base is one hover away.
def _format_byte_value(value, hint):
    if hint == "binary":
        return f"%{value:08b}"
    if hint == "decimal":
        return str(value)
    if hint == "char":
        if 32 <= value <= 126 and value not in (39, 92):
            return f"'{chr(value)}'"
        return f"&{value:02X}"
    if hint == "inkey":
        return f"{value - 256}" if value >= 128 else str(value)
    if hint == "octal":
        return f"&O{value:o}"
    return f"&{value:02X}"


def _format_word_value(value, hint):
    if hint == "binary":
        return f"%{value:016b}"
    if hint == "decimal":
        return str(value)
    if hint == "octal":
        return f"&O{value:o}"
    return f"&{value:04X}"


def _hinted_value_width(item):
    """Widest value width across an item's elements after format_hints.

    Used by `_optimal_data_max_width` to balance multi-line data
    layouts against the comment column. Returns the default hex width
    (3 for byte, 5 for word) when no hint widens any element.
    """
    base_w = 3 if item["type"] == "byte" else 5
    hints = item.get("format_hints")
    if not hints:
        return base_w
    formatter = _format_byte_value if item["type"] == "byte" else _format_word_value
    values = item.get("values", [])
    widths = [base_w]
    for i, v in enumerate(values):
        h = hints[i] if i < len(hints) else None
        if h is not None:
            widths.append(len(formatter(v, h)))
    return max(widths)


def _render_bytes(item, max_width=CONTENT_MAX_WIDTH):
    values = item.get("values", [])
    expressions = item.get("expressions")
    format_hints = item.get("format_hints")
    parts = []
    widths = []
    for i, v in enumerate(values):
        expr = expressions[i] if expressions and i < len(expressions) else None
        hint = format_hints[i] if format_hints and i < len(format_hints) else None
        tooltip = _immediate_tooltip(v)
        if expr:
            parts.append(
                f'<span data-tip="{escape(tooltip)}">'
                f'{escape(expr)}</span>'
            )
            widths.append(len(expr))
        else:
            display = _format_byte_value(v, hint)
            parts.append(
                f'<span data-tip="{escape(tooltip)}">'
                f'{escape(display)}</span>'
            )
            widths.append(len(display))
    prefix_html = '    <span class="directive">EQUB</span> '
    prefix_width = 9  # visible "    EQUB "
    line_groups = _group_values(parts, prefix_width, 3, max_width,
                                widths=widths)
    indent = " " * prefix_width
    joined_groups = [", ".join(group) for group in line_groups]
    all_html = (",\n" + indent).join(joined_groups)
    return Markup(prefix_html + all_html)


def _render_fill(item):
    """Render a Fill item as `FILL N × &XX` (an N-byte run of a constant).

    The underlying py8dis disassembler emits this as a compact beebasm
    FOR/NEXT loop; we don't need to reproduce the FOR syntax in the
    pretty output, just tell the reader how many bytes are being
    filled with what value.
    """
    value = item.get("value", 0)
    length = item.get("length", 0)
    tooltip = _immediate_tooltip(value)
    value_html = (
        f'<span data-tip="{escape(tooltip)}">&amp;{value:02X}</span>'
    )
    return Markup(
        f'    <span class="directive">FILL</span> '
        f'{length} &times; {value_html}'
    )


def _render_words(item, max_width=CONTENT_MAX_WIDTH):
    values = item.get("values", [])
    expressions = item.get("expressions")
    format_hints = item.get("format_hints")
    parts = []
    widths = []
    for i, v in enumerate(values):
        expr = expressions[i] if expressions and i < len(expressions) else None
        hint = format_hints[i] if format_hints and i < len(format_hints) else None
        if expr:
            tooltip = f"&amp;{v:04X}"
            parts.append(
                f'<span data-tip="{tooltip}">{escape(expr)}</span>'
            )
            widths.append(len(expr))
        else:
            display = _format_word_value(v, hint)
            parts.append(str(escape(display)))
            widths.append(len(display))
    prefix_html = '    <span class="directive">EQUW</span> '
    prefix_width = 9  # visible "    EQUW "
    line_groups = _group_values(parts, prefix_width, 5, max_width,
                                widths=widths)
    indent = " " * prefix_width
    joined_groups = [", ".join(group) for group in line_groups]
    all_html = (",\n" + indent).join(joined_groups)
    return Markup(prefix_html + all_html)


def _render_string(item):
    string = item.get("string", "")
    return Markup(
        f'    <span class="directive">EQUS</span>'
        f' <span class="string">&quot;{escape(string)}&quot;</span>'
    )
