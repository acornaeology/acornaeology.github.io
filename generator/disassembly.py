"""Processes structured disassembly JSON into template-ready data."""

import re

from markupsafe import Markup, escape


def process_disassembly(data):
    """Process structured JSON into a list of template-ready lines.

    Each line is a dict with:
        id   - HTML id attribute (str or None)
        addr - address display string (str or None)
        html - pre-rendered content (Markup)
        hex  - hex byte display (str or None)
        banner - True if this is a full-width subroutine header (optional)
    """
    sub_lookup = {}
    for sub in data.get("subroutines", []):
        sub_lookup[sub["addr"]] = sub

    lines = []
    for item in data["items"]:
        lines.extend(_process_item(item, sub_lookup))

    _align_inline_comments(lines)
    return lines


def _process_item(item, sub_lookup):
    lines = []
    addr = item["addr"]
    addr_id = f"addr-{addr:04X}"
    addr_display = f"{addr:04X}"
    addr_used = False

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
            "hex": None,
            "banner": True,
        })
        addr_used = True

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
    for label_name in item.get("labels", []):
        lines.append({
            "id": addr_id if not addr_used else None,
            "addr": addr_display if not addr_used else None,
            "html": Markup(f'<span class="label">.{escape(label_name)}</span>'),
            "hex": None,
        })
        addr_used = True

    # Main content line — store inline comment separately for alignment
    content_html = _render_content(item)
    hex_str = " ".join(f"{b:02X}" for b in item["bytes"])

    lines.append({
        "id": addr_id if not addr_used else None,
        "addr": addr_display if not addr_used else None,
        "html": content_html,
        "hex": hex_str,
        "_inline_comment": item.get("comment_inline"),
    })

    # Comments after (rare)
    for comment_text in item.get("comments_after", []):
        _append_comment_lines(lines, comment_text)

    return lines


def _visible_width(html):
    """Compute the visible character width of an HTML string."""
    text = re.sub(r"<[^>]+>", "", str(html))
    # Decode HTML entities
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"')
    return len(text)


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

        # Merge comments with padding
        for i, line in commented:
            comment = line.pop("_inline_comment")
            code_width = _visible_width(line["html"])
            padding = " " * (max_width - code_width + 2)
            line["html"] = line["html"] + Markup(
                f'{padding}<span class="comment">; {escape(comment)}</span>'
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
    for line_text in str(comment_text).split("\n"):
        if line_text.strip():
            html = Markup(
                f'<span class="comment">; {escape(line_text)}</span>'
            )
        else:
            html = Markup("")
        lines.append({"id": None, "addr": None, "html": html, "hex": None})


def _empty_line():
    return {"id": None, "addr": None, "html": Markup(""), "hex": None}


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
        parts.append('<div class="sub-registers">')
        if entry:
            parts.append(_render_register_table("On Entry", entry))
        if exit_:
            parts.append(_render_register_table("On Exit", exit_))
        parts.append("</div>")

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


def _render_register_table(heading, regs):
    """Render a register table (on_entry or on_exit) as a definition list."""
    parts = [f"<h4>{escape(heading)}</h4>", "<dl>"]
    for reg, desc in regs.items():
        parts.append(
            f"<dt>{escape(reg.upper())}</dt>"
            f"<dd>{escape(desc)}</dd>"
        )
    parts.append("</dl>")
    return "\n".join(parts)


def _render_content(item):
    t = item["type"]
    if t == "code":
        return _render_code(item)
    if t == "byte":
        return _render_bytes(item)
    if t == "word":
        return _render_words(item)
    if t == "string":
        return _render_string(item)
    return Markup("")


def _render_code(item):
    mnemonic = escape(item["mnemonic"].upper())
    operand = item.get("operand", "")

    html = Markup(f'    <span class="opcode">{mnemonic}</span>')
    if operand:
        operand_html = _linkify_operand(operand, item)
        if _is_immediate(operand, item):
            tooltip = _immediate_tooltip(item["bytes"][1])
            operand_html = Markup(
                f'<span class="imm" data-tip="{escape(tooltip)}">'
                f'{operand_html}</span>'
            )
        html += Markup(f' <span class="operand">{operand_html}</span>')
    return html


def _linkify_operand(operand, item):
    """Wrap label references in the operand text with anchor links."""
    if "target_label" not in item or "target" not in item:
        return escape(operand)

    target_label = item["target_label"]
    target_id = f"addr-{item['target']:04X}"

    escaped_operand = str(escape(operand))
    escaped_label = str(escape(target_label))

    if escaped_label in escaped_operand:
        target_addr = f"&{item['target']:04X}"
        link = (f'<a href="#{target_id}"'
                f' data-tip="{target_addr}">{escaped_label}</a>')
        return Markup(escaped_operand.replace(escaped_label, link, 1))

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
    formatted = ", ".join(f"&{v:02X}" for v in values)
    return Markup(
        f'    <span class="directive">EQUB</span> {escape(formatted)}'
    )


def _render_words(item):
    values = item.get("values", [])
    formatted = ", ".join(f"&{v:04X}" for v in values)
    return Markup(
        f'    <span class="directive">EQUW</span> {escape(formatted)}'
    )


def _render_string(item):
    string = item.get("string", "")
    return Markup(
        f'    <span class="directive">EQUS</span>'
        f' <span class="string">&quot;{escape(string)}&quot;</span>'
    )
