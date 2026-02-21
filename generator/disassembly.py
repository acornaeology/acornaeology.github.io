"""Processes structured disassembly JSON into template-ready data."""

import html as html_mod
import re

from markupsafe import Markup, escape

CONTENT_MAX_WIDTH = 64


def process_disassembly(data):
    """Process structured JSON into a list of template-ready lines.

    Each line is a dict with:
        id   - HTML id attribute (str or None)
        addr - address display string (str or None)
        html - pre-rendered content (Markup)
        banner - True if this is a full-width subroutine header (optional)
    """
    sub_lookup = {}
    for sub in data.get("subroutines", []):
        sub_lookup[sub["addr"]] = sub

    item_by_addr = {item["addr"]: item for item in data["items"]}
    valid_addrs = set(item_by_addr)

    lines = []
    for item in data["items"]:
        lines.extend(_process_item(item, sub_lookup, item_by_addr, valid_addrs))

    _align_inline_comments(lines)
    return lines


def _process_item(item, sub_lookup, item_by_addr, valid_addrs):
    lines = []
    addr = item["addr"]
    addr_id = f"addr-{addr:04X}"
    addr_display = f"{addr:04X}"
    id_used = False
    addr_shown = False

    sub = sub_lookup.get(addr)

    # Filter comments
    comments = [
        c for c in item.get("comments_before", [])
        if not _is_reference_comment(c) and not _is_banner_line(c)
    ]

    if sub and sub.get("title"):
        # Render structured subroutine header instead of raw comments
        lines.append(_empty_line())
        lines.append({
            "id": addr_id,
            "addr": None,
            "html": _render_subroutine_header(sub),
            "banner": True,
        })
        id_used = True

        # Render any comments that aren't part of the banner block
        non_banner = [c for c in comments if not _is_banner_content(c, sub)]
        for comment_text in non_banner:
            _append_comment_lines(lines, comment_text)
    else:
        # Normal comment rendering
        if comments:
            lines.append(_empty_line())
        for comment_text in comments:
            _append_comment_lines(lines, comment_text)

    # Label lines
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
            "html": label_html,
        })
        id_used = True
        addr_shown = True

    # Main content line — store inline comment separately for alignment
    content_html = _render_content(item, valid_addrs)

    lines.append({
        "id": addr_id if not id_used else None,
        "addr": addr_display if not addr_shown else None,
        "addr_id": addr_id,
        "html": content_html,
        "_inline_comment": item.get("comment_inline"),
    })

    # Comments after (rare)
    for comment_text in item.get("comments_after", []):
        _append_comment_lines(lines, comment_text)

    return lines


def _visible_width(markup):
    """Compute the visible character width of an HTML string."""
    text = re.sub(r"<[^>]+>", "", str(markup))
    text = html_mod.unescape(text)
    return len(text)


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


def _wrap_text(text, first_line_budget, continuation_indent):
    """Wrap plain text at word boundaries, returning a list of strings.

    Words longer than the budget are broken at preferred positions."""
    words = text.split(" ")
    result_lines = []
    current = ""
    continuation_budget = CONTENT_MAX_WIDTH - continuation_indent

    for word in words:
        budget = first_line_budget if not result_lines else continuation_budget

        candidate = current + (" " if current else "") + word
        if len(candidate) <= budget:
            current = candidate
        else:
            if current:
                result_lines.append(current)
                current = ""
                budget = continuation_budget

            # Break words that don't fit on a fresh line
            while len(word) > budget:
                pos = _find_break_position(word, budget)
                result_lines.append(word[:pos])
                word = word[pos:]
                budget = continuation_budget
            current = word

    if current:
        result_lines.append(current)

    return result_lines or [""]


def _group_values(parts, prefix_width, value_width):
    """Group data values into lines that fit within CONTENT_MAX_WIDTH.

    Returns a list of lists (groups of parts per line)."""
    line_groups = []
    current_group = []
    current_width = prefix_width

    for part in parts:
        needed = (2 if current_group else 0) + value_width
        if current_width + needed > CONTENT_MAX_WIDTH and current_group:
            line_groups.append(current_group)
            current_group = [part]
            current_width = prefix_width + value_width
        else:
            current_group.append(part)
            current_width += needed
    if current_group:
        line_groups.append(current_group)

    return line_groups


def _align_inline_comments(lines):
    """Align inline comments within blocks separated by labels/banners.

    Within each block, all inline comments start at the same column —
    the position of the widest code line in that block, plus padding."""
    blocks = _split_into_blocks(lines)

    for block in blocks:
        # Find lines with inline comments and the max code width in this block
        commented = [(i, line) for i, line in block
                     if line.get("_inline_comment")]
        if not commented:
            continue

        max_width = max(_visible_width(line["html"]) for _, line in commented)

        # Merge comments with padding, wrapping if needed
        for i, line in commented:
            comment = line.pop("_inline_comment")
            code_width = _visible_width(line["html"])
            padding = " " * (max_width - code_width + 2)
            comment_col = max_width + 2 + 2  # padding + "; "
            total_width = comment_col + len(comment)

            if total_width <= CONTENT_MAX_WIDTH:
                line["html"] = line["html"] + Markup(
                    f'{padding}<span class="comment">'
                    f'; {escape(comment)}</span>'
                )
            else:
                first_budget = CONTENT_MAX_WIDTH - comment_col
                wrapped = _wrap_text(comment, first_budget, comment_col)
                indent_str = " " * comment_col
                parts = [str(escape(wrapped[0]))]
                for cont in wrapped[1:]:
                    parts.append(f"\n{indent_str}{escape(cont)}")
                comment_html = "".join(parts)
                line["html"] = line["html"] + Markup(
                    f'{padding}<span class="comment">'
                    f'; {comment_html}</span>'
                )

    # Clean up: remove _inline_comment from any remaining lines
    for line in lines:
        line.pop("_inline_comment", None)


def _split_into_blocks(lines):
    """Split lines into blocks at label boundaries and subroutine headers.

    Returns a list of blocks, where each block is a list of (index, line)
    tuples."""
    blocks = []
    current = []

    for i, line in enumerate(lines):
        # Start a new block at labels, banners, or blank separators
        is_label = '<span class="label">' in str(line.get("html", ""))
        is_banner = line.get("banner")

        if is_label or is_banner:
            if current:
                blocks.append(current)
            current = [(i, line)]
        else:
            current.append((i, line))

    if current:
        blocks.append(current)

    return blocks


def _append_comment_lines(lines, comment_text):
    comment_prefix_width = 2  # "; "
    for line_text in str(comment_text).split("\n"):
        if not line_text.strip():
            lines.append({"id": None, "addr": None, "html": Markup("")})
            continue

        # Skip wrapping for indented lines (preformatted content)
        if line_text.startswith("  "):
            html = Markup(
                f'<span class="comment">; {escape(line_text)}</span>'
            )
            lines.append({"id": None, "addr": None, "html": html})
            continue

        total_width = comment_prefix_width + len(line_text)
        if total_width <= CONTENT_MAX_WIDTH:
            html = Markup(
                f'<span class="comment">; {escape(line_text)}</span>'
            )
            lines.append({"id": None, "addr": None, "html": html})
        else:
            budget = CONTENT_MAX_WIDTH - comment_prefix_width
            wrapped = _wrap_text(line_text, budget, comment_prefix_width)
            parts = [str(escape(wrapped[0]))]
            for cont in wrapped[1:]:
                parts.append(f"\n; {escape(cont)}")
            comment_html = "".join(parts)
            html = Markup(
                f'<span class="comment">; {comment_html}</span>'
            )
            lines.append({"id": None, "addr": None, "html": html})


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


def _render_subroutine_header(sub):
    """Render a subroutine's structured data as a styled HTML block."""
    parts = []
    parts.append('<div class="sub-header">')

    # Title
    title = sub.get("title", sub.get("name", ""))
    parts.append(f'<h3>{escape(title)}</h3>')

    # Description
    desc = sub.get("description", "")
    if desc:
        parts.append(f'<div class="sub-desc">{_render_plaintext(desc)}</div>')

    # On Entry / On Exit
    entry = sub.get("on_entry", {})
    exit_ = sub.get("on_exit", {})
    if entry or exit_:
        parts.append('<div class="sub-registers"><table>')
        if entry:
            parts.append(_render_register_rows("On Entry", entry))
        if exit_:
            parts.append(_render_register_rows("On Exit", exit_))
        parts.append("</table></div>")

    parts.append("</div>")
    return Markup("\n".join(parts))


def _render_plaintext(text):
    """Render plain text as HTML, preserving the author's intended structure.

    Blank-line-separated blocks become paragraphs or preformatted blocks.
    A block where every line is indented (2+ spaces) is rendered as <pre>.
    Everything else becomes a <p> with line breaks preserved."""
    parts = []
    # Split on blank lines
    blocks = []
    current = []
    for line in text.split("\n"):
        if not line.strip():
            if current:
                blocks.append(current)
                current = []
        else:
            current.append(line)
    if current:
        blocks.append(current)

    for block in blocks:
        if all(line.startswith("  ") for line in block):
            # Indented block -> preformatted
            content = "\n".join(block)
            parts.append(f'<pre class="sub-detail">{escape(content)}</pre>')
        else:
            # Prose block -- join hard-wrapped lines with spaces
            content = " ".join(line.strip() for line in block)
            parts.append(f"<p>{escape(content)}</p>")

    return Markup("\n".join(parts))


def _render_register_rows(heading, regs):
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
            f"<td>{escape(desc)}</td></tr>"
        )
    return "\n".join(rows)


def _render_content(item, valid_addrs):
    t = item["type"]
    if t == "code":
        return _render_code(item, valid_addrs)
    if t == "byte":
        return _render_bytes(item)
    if t == "word":
        return _render_words(item)
    if t == "string":
        return _render_string(item)
    return Markup("")


def _render_code(item, valid_addrs):
    mnemonic = escape(item["mnemonic"].upper())
    operand = item.get("operand", "")

    html = Markup(f'    <span class="opcode">{mnemonic}</span>')
    if operand:
        operand_html = _linkify_operand(operand, item, valid_addrs)
        if _is_immediate(operand, item):
            tooltip = _immediate_tooltip(item["bytes"][1])
            operand_html = Markup(
                f'<span class="imm" data-tip="{escape(tooltip)}">'
                f'{operand_html}</span>'
            )
        html += Markup(f' <span class="operand">{operand_html}</span>')
    return html


def _linkify_operand(operand, item, valid_addrs):
    """Wrap label references in the operand text with anchor links."""
    if "target_label" not in item or "target" not in item:
        return escape(operand)

    target_label = item["target_label"]
    target = item["target"]
    target_addr = f"&{target:04X}"

    escaped_operand = str(escape(operand))
    escaped_label = str(escape(target_label))

    if escaped_label in escaped_operand:
        if target in valid_addrs:
            target_id = f"addr-{target:04X}"
            replacement = (f'<a href="#{target_id}"'
                           f' data-tip="{target_addr}">{escaped_label}</a>')
        else:
            replacement = (f'<span class="ext-label"'
                           f' data-tip="{target_addr}">{escaped_label}</span>')
        return Markup(escaped_operand.replace(escaped_label, replacement, 1))

    return escape(operand)


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


def _render_bytes(item):
    values = item.get("values", [])
    parts = []
    for v in values:
        tooltip = _immediate_tooltip(v)
        parts.append(
            f'<span data-tip="{escape(tooltip)}">&amp;{v:02X}</span>'
        )
    prefix_html = '    <span class="directive">EQUB</span> '
    prefix_width = 9  # visible "    EQUB "
    line_groups = _group_values(parts, prefix_width, 3)
    indent = " " * prefix_width
    joined_groups = [", ".join(group) for group in line_groups]
    all_html = (",\n" + indent).join(joined_groups)
    return Markup(prefix_html + all_html)


def _render_words(item):
    values = item.get("values", [])
    escaped_parts = [str(escape(f"&{v:04X}")) for v in values]
    prefix_html = '    <span class="directive">EQUW</span> '
    prefix_width = 9  # visible "    EQUW "
    line_groups = _group_values(escaped_parts, prefix_width, 5)
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
