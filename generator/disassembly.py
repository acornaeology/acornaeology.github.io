"""Processes structured disassembly JSON into template-ready data."""

from markupsafe import Markup, escape


def process_disassembly(data):
    """Process structured JSON into a list of template-ready lines.

    Each line is a dict with:
        id   - HTML id attribute (str or None)
        addr - address display string (str or None)
        html - pre-rendered content (Markup)
        hex  - hex byte display (str or None)
    """
    lines = []
    for item in data["items"]:
        lines.extend(_process_item(item))
    return lines


def _process_item(item):
    lines = []
    addr = item["addr"]
    addr_id = f"addr-{addr:04X}"
    addr_display = f"{addr:04X}"
    addr_used = False

    # Blank separator before items with comments (subroutine boundaries)
    comments = [
        c for c in item.get("comments_before", [])
        if not _is_reference_comment(c)
    ]
    if comments:
        lines.append(_empty_line())

    # Comment lines
    for comment_text in comments:
        for line_text in str(comment_text).split("\n"):
            if line_text.strip():
                html = Markup(
                    f'<span class="comment">; {escape(line_text)}</span>'
                )
            else:
                html = Markup("")
            lines.append({"id": None, "addr": None, "html": html, "hex": None})

    # Label lines
    for label_name in item.get("labels", []):
        lines.append({
            "id": addr_id if not addr_used else None,
            "addr": addr_display if not addr_used else None,
            "html": Markup(f'<span class="label">.{escape(label_name)}</span>'),
            "hex": None,
        })
        addr_used = True

    # Main content line
    content_html = _render_content(item)
    hex_str = " ".join(f"{b:02X}" for b in item["bytes"])

    inline = item.get("comment_inline")
    if inline:
        content_html += Markup(
            f'  <span class="comment">; {escape(inline)}</span>'
        )

    lines.append({
        "id": addr_id if not addr_used else None,
        "addr": addr_display if not addr_used else None,
        "html": content_html,
        "hex": hex_str,
    })

    # Comments after (rare)
    for comment_text in item.get("comments_after", []):
        for line_text in str(comment_text).split("\n"):
            if line_text.strip():
                html = Markup(
                    f'<span class="comment">; {escape(line_text)}</span>'
                )
                lines.append(
                    {"id": None, "addr": None, "html": html, "hex": None}
                )

    return lines


def _empty_line():
    return {"id": None, "addr": None, "html": Markup(""), "hex": None}


def _is_reference_comment(text):
    """Auto-generated cross-reference comments are redundant with the
    structured references field."""
    return text.startswith("&") and "referenced" in text


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
        link = f'<a href="#{target_id}">{escaped_label}</a>'
        return Markup(escaped_operand.replace(escaped_label, link, 1))

    return escape(operand)


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
