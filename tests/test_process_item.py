"""Integration tests for `_process_item`.

These exercise the row-emission pipeline end-to-end against
hand-crafted item / sub dicts. They cover:

- Per-align comment routing (dasmos 1.5 fields)
- Backward-compat with old `comments_before` / `comments_after`
- Banner card placement based on the new `align` field
- ATX heading promotion in each per-align bucket
- Section-break flag (BEFORE_LABEL banners only)
- format_hints integration in the data row

The renderer returns a list of line dicts. Tests inspect that
intermediate structure (id / banner / section_break / html keys)
rather than the final HTML, which keeps the assertions resilient to
template changes.
"""

from __future__ import annotations

import pytest

from generator.disassembly import _process_item


def _process(item, sub=None, max_width=64):
    """Convenience wrapper: build the per-call lookups and dispatch."""
    sub_lookup = {}
    if sub is not None:
        key = (sub.get("addr", item["addr"]), sub.get("binary_addr"))
        sub_lookup[key] = sub
    item_by_addr = {item["addr"]: item}
    valid_addrs = {item["addr"]}
    sorted_addrs = sorted(valid_addrs)
    return _process_item(
        item=item,
        sub_lookup=sub_lookup,
        item_by_addr=item_by_addr,
        valid_addrs=valid_addrs,
        sorted_addrs=sorted_addrs,
        label_tooltips={},
        mm_links={},
        max_width=max_width,
    )


def _classify(line):
    """Tag each line with a coarse role for readable assertions."""
    if line.get("banner"):
        return "banner"
    html = str(line.get("html", ""))
    if html == "":
        return "empty"
    if 'class="label"' in html:
        return "label"
    if 'class="comment"' in html:
        return "comment"
    if 'class="directive"' in html or 'class="opcode"' in html:
        return "data"
    return "other"


class TestPerAlignCommentRouting:
    """dasmos 1.5 splits comments_before / comments_after into four
    per-align fields. Each routes to a distinct row-group position.
    """

    def test_before_label_above_label(self):
        item = {
            "addr": 0x8000,
            "type": "byte",
            "values": [0x42],
            "labels": ["foo"],
            "comments_before_label": ["before label"],
        }
        roles = [_classify(l) for l in _process(item)]
        # empty, comment, label, data
        assert roles == ["empty", "comment", "label", "data"]

    def test_after_label_between_label_and_data(self):
        item = {
            "addr": 0x8000,
            "type": "byte",
            "values": [0x42],
            "labels": ["foo"],
            "comments_after_label": ["after label"],
        }
        roles = [_classify(l) for l in _process(item)]
        # No leading empty line: items with only AFTER-* decorations
        # rely on the banner card / per-row CSS for visual separation
        # from the previous item, matching the historical rendering.
        assert roles == ["label", "comment", "data"]

    def test_before_line_between_label_and_data(self):
        item = {
            "addr": 0x8000,
            "type": "byte",
            "values": [0x42],
            "labels": ["foo"],
            "comments_before_line": ["before line"],
        }
        roles = [_classify(l) for l in _process(item)]
        # BEFORE_LINE shares the AFTER_LABEL slot in the current renderer
        assert roles == ["label", "comment", "data"]

    def test_after_line_below_data(self):
        item = {
            "addr": 0x8000,
            "type": "byte",
            "values": [0x42],
            "labels": ["foo"],
            "comments_after_line": ["after line"],
        }
        roles = [_classify(l) for l in _process(item)]
        assert roles == ["label", "data", "comment"]

    def test_no_comments_no_empty_line(self):
        item = {
            "addr": 0x8000, "type": "byte", "values": [0x42],
            "labels": ["foo"],
        }
        roles = [_classify(l) for l in _process(item)]
        assert "empty" not in roles
        assert roles == ["label", "data"]

    def test_all_four_buckets_route_correctly(self):
        item = {
            "addr": 0x8000, "type": "byte", "values": [0x42],
            "labels": ["foo"],
            "comments_before_label": ["BL"],
            "comments_after_label": ["AL"],
            "comments_before_line": ["BLN"],
            "comments_after_line": ["ALN"],
        }
        lines = _process(item)
        roles = [_classify(l) for l in lines]
        # empty (because BL is present), BL, label, AL, BLN, data, ALN
        assert roles == [
            "empty", "comment", "label",
            "comment", "comment", "data", "comment",
        ]
        # Each comment row carries the corresponding text
        comment_lines = [l for l in lines if _classify(l) == "comment"]
        comment_html = [str(l["html"]) for l in comment_lines]
        assert "BL" in comment_html[0]
        assert "AL" in comment_html[1]
        assert "BLN" in comment_html[2]
        assert "ALN" in comment_html[3]


class TestBackwardCompatOldFieldNames:
    """Sources still on dasmos < 1.5 emit the conflated `comments_before`
    / `comments_after` fields. Those route to BEFORE_LABEL and AFTER_LINE
    buckets respectively, matching the historical default placement.
    """

    def test_old_comments_before_routes_to_before_label(self):
        item = {
            "addr": 0x8000, "type": "byte", "values": [0x42],
            "labels": ["foo"],
            "comments_before": ["old-shape comment"],
        }
        roles = [_classify(l) for l in _process(item)]
        assert roles == ["empty", "comment", "label", "data"]

    def test_old_comments_after_routes_to_after_line(self):
        item = {
            "addr": 0x8000, "type": "byte", "values": [0x42],
            "labels": ["foo"],
            "comments_after": ["old-shape after"],
        }
        roles = [_classify(l) for l in _process(item)]
        assert roles == ["label", "data", "comment"]

    def test_new_field_takes_precedence_over_old(self):
        # If both `comments_before_label` and the legacy `comments_before`
        # are present, the new field wins (dasmos 1.5 dropped the old
        # field, so this should rarely happen, but the precedence is
        # well-defined.)
        item = {
            "addr": 0x8000, "type": "byte", "values": [0x42],
            "labels": ["foo"],
            "comments_before_label": ["NEW"],
            "comments_before": ["OLD"],
        }
        lines = _process(item)
        comment_lines = [l for l in lines if _classify(l) == "comment"]
        assert len(comment_lines) == 1
        assert "NEW" in str(comment_lines[0]["html"])
        assert "OLD" not in str(comment_lines[0]["html"])


class TestBannerAlignment:
    """Banner records carry an `align` field in dasmos 1.5; the
    sub-header card is placed accordingly relative to the label rows.
    """

    def test_before_label_banner_above_label(self):
        item = {
            "addr": 0x8000, "type": "byte", "values": [0x42],
            "labels": ["foo"],
        }
        sub = {"addr": 0x8000, "title": "Heading", "align": "before_label"}
        lines = _process(item, sub=sub)
        roles = [_classify(l) for l in lines]
        assert roles == ["empty", "banner", "label", "data"]

    def test_after_label_banner_between_label_and_data(self):
        # AFTER_LABEL banners render as code-style block comments
        # (monospace `; ` rows) rather than as styled sub-header cards.
        # Mid-item card treatment visually disrupts the listing flow;
        # the inline-comment treatment reads naturally with the
        # surrounding code rows. Title gets `**bold**` emphasis.
        item = {
            "addr": 0x8000, "type": "byte", "values": [0x42],
            "labels": ["foo"],
        }
        sub = {"addr": 0x8000, "title": "Heading", "align": "after_label"}
        lines = _process(item, sub=sub)
        roles = [_classify(l) for l in lines]
        assert roles == ["label", "comment", "data"]
        # Title rendered with bold emphasis through Markdown.
        comment_html = next(
            str(l["html"]) for l in lines if _classify(l) == "comment")
        assert "<strong>" in comment_html
        assert "Heading" in comment_html

    def test_after_line_banner_below_data(self):
        item = {
            "addr": 0x8000, "type": "byte", "values": [0x42],
            "labels": ["foo"],
        }
        sub = {"addr": 0x8000, "title": "Heading", "align": "after_line"}
        lines = _process(item, sub=sub)
        roles = [_classify(l) for l in lines]
        # No empty line at top (after-line banner doesn't decorate above)
        assert roles == ["label", "data", "banner"]

    def test_default_align_is_before_label(self):
        # Sources without dasmos 1.5 don't emit `align`; default matches
        # the historical hard-coded "above the label" placement.
        item = {
            "addr": 0x8000, "type": "byte", "values": [0x42],
            "labels": ["foo"],
        }
        sub = {"addr": 0x8000, "title": "Heading"}  # no align field
        roles = [_classify(l) for l in _process(item, sub=sub)]
        assert roles == ["empty", "banner", "label", "data"]


class TestSectionBreakFlag:
    """Only BEFORE_LABEL-position banners trigger a section split. After
    label / after line banners stay in the same section as their item's
    label and data rows.
    """

    def test_before_label_banner_carries_section_break(self):
        item = {
            "addr": 0x8000, "type": "byte", "values": [0x42],
            "labels": ["foo"],
        }
        sub = {"addr": 0x8000, "title": "X", "align": "before_label"}
        lines = _process(item, sub=sub)
        banner_lines = [l for l in lines if l.get("banner")]
        assert all(l.get("section_break") for l in banner_lines)

    def test_after_label_banner_emits_no_banner_rows(self):
        # AFTER_LABEL banners render through the comment pipeline, so
        # there are no `banner: True` rows for them at all -- and
        # therefore no row that could carry section_break=True.
        # Sections cannot split between this item's labels and data.
        item = {
            "addr": 0x8000, "type": "byte", "values": [0x42],
            "labels": ["foo"],
        }
        sub = {"addr": 0x8000, "title": "X", "align": "after_label"}
        lines = _process(item, sub=sub)
        assert not any(l.get("banner") for l in lines)
        assert not any(l.get("section_break") for l in lines)

    def test_atx_heading_in_before_label_carries_section_break(self):
        item = {
            "addr": 0x8000, "type": "byte", "values": [0x42],
            "labels": ["foo"],
            "comments_before_label": ["# Section heading"],
        }
        lines = _process(item)
        # Heading row sits in the banner role
        heading = [l for l in lines if l.get("banner")]
        assert len(heading) == 1
        assert heading[0].get("section_break") is True

    def test_atx_heading_in_after_label_no_section_break(self):
        item = {
            "addr": 0x8000, "type": "byte", "values": [0x42],
            "labels": ["foo"],
            "comments_after_label": ["## Mid-item heading"],
        }
        lines = _process(item)
        heading = [l for l in lines if l.get("banner")]
        assert len(heading) == 1
        assert not heading[0].get("section_break")


class TestAtxHeadingPromotion:
    """ATX heading comments (`# title`) render as banner-style heading
    rows rather than going through the inline-Markdown path (which
    would mangle the resulting <hN> tag).
    """

    def test_h1_in_before_label_renders_as_h2(self):
        # Page already has an <h1>; comments start at <h2>.
        item = {
            "addr": 0x8000, "type": "byte", "values": [0x42],
            "labels": ["foo"],
            "comments_before_label": ["# Top"],
        }
        lines = _process(item)
        heading = next(l for l in lines if l.get("banner"))
        assert "<h2>" in str(heading["html"])
        assert "Top" in str(heading["html"])

    def test_h2_renders_as_h3(self):
        item = {
            "addr": 0x8000, "type": "byte", "values": [0x42],
            "labels": ["foo"],
            "comments_before_label": ["## Sub"],
        }
        lines = _process(item)
        heading = next(l for l in lines if l.get("banner"))
        assert "<h3>" in str(heading["html"])

    def test_heading_does_not_render_as_comment_row(self):
        # The bug being prevented: `<h1>` content leaking into a
        # comment-cell <span> where the visible-width wrapper strips
        # the angle brackets.
        item = {
            "addr": 0x8000, "type": "byte", "values": [0x42],
            "labels": ["foo"],
            "comments_before_label": ["# Title"],
        }
        lines = _process(item)
        for line in lines:
            html = str(line.get("html", ""))
            assert "h1>" not in html or "<h" in html  # no orphan tag fragments
            assert "h2>" not in html.replace("<h2>", "").replace("</h2>", "")


class TestIdAndAddrPlacement:
    """The first emitted row claims the `id` (so `#addr-XXXX` scrolls
    to the top of the item's rendering); the address column "8000"
    appears once, on the first row that should display it.
    """

    def test_id_on_label_when_no_decoration(self):
        item = {
            "addr": 0x8000, "type": "byte", "values": [0x42],
            "labels": ["foo"],
        }
        lines = _process(item)
        ids = [l.get("id") for l in lines]
        assert ids == ["addr-8000", None]

    def test_id_on_banner_when_present(self):
        item = {
            "addr": 0x8000, "type": "byte", "values": [0x42],
            "labels": ["foo"],
        }
        sub = {"addr": 0x8000, "title": "X", "align": "before_label"}
        lines = _process(item, sub=sub)
        # empty, banner, label, data — id on the banner row
        assert lines[0]["id"] is None  # empty row
        assert lines[1]["id"] == "addr-8000"
        assert lines[2]["id"] is None
        assert lines[3]["id"] is None

    def test_after_label_banner_id_on_label(self):
        # If banner is after-label, label row is first → label gets id.
        item = {
            "addr": 0x8000, "type": "byte", "values": [0x42],
            "labels": ["foo"],
        }
        sub = {"addr": 0x8000, "title": "X", "align": "after_label"}
        lines = _process(item, sub=sub)
        # label, banner, data — no leading empty line
        assert lines[0]["id"] == "addr-8000"
        assert lines[1]["id"] is None
        assert lines[2]["id"] is None

    def test_addr_column_shown_once(self):
        item = {
            "addr": 0x8000, "type": "byte", "values": [0x42],
            "labels": ["foo", "bar"],
        }
        lines = _process(item)
        addrs = [l.get("addr") for l in lines]
        # First label gets "8000"; subsequent rows None.
        assert addrs.count("8000") == 1


class TestFormatHintsInDataRow:
    """format_hints carries through into the rendered data row."""

    def test_binary_hint_renders_as_binary(self):
        item = {
            "addr": 0x8006, "type": "byte", "values": [0x82],
            "labels": ["rom_type"],
            "format_hints": ["binary"],
            "comment_inline": "ROM type",
        }
        lines = _process(item)
        data_lines = [l for l in lines if _classify(l) == "data"]
        assert len(data_lines) == 1
        assert "%10000010" in str(data_lines[0]["html"])


class TestProseBlankLineCollapsing:
    """Consecutive blank lines within a single comment collapse to a
    single blank row. Prevents "\\n\\n" paragraph breaks from stacking
    multiple empty rows in the listing.
    """

    def test_paragraph_break_emits_one_blank_row(self):
        item = {
            "addr": 0x8000, "type": "byte", "values": [0x42],
            "labels": ["foo"],
            "comments_before_label": ["First para\n\nSecond para"],
        }
        roles = [_classify(l) for l in _process(item)]
        # empty (preamble), comment, empty (paragraph break), comment,
        # label, data
        assert roles == [
            "empty", "comment", "empty", "comment", "label", "data",
        ]

    def test_trailing_blank_lines_collapse_to_one(self):
        item = {
            "addr": 0x8000, "type": "byte", "values": [0x42],
            "labels": ["foo"],
            "comments_before_label": ["text\n\n\n\n"],
        }
        roles = [_classify(l) for l in _process(item)]
        assert roles.count("empty") <= 2  # at most preamble + one trailing

    def test_banner_with_table_has_one_blank_above_table(self):
        # The combined "**title**\n\n| ... |" payload produced by
        # _append_banner_as_block_comment lays out as:
        #   ; **title**
        #   ;
        #   ; ┌─...─┐ (table top border)
        # not with two blank rows between title and table.
        item = {
            "addr": 0x8000, "type": "byte", "values": [0x42],
            "labels": ["foo"],
        }
        sub = {
            "addr": 0x8000, "title": "Title here", "align": "after_label",
            "description": "| A |\n|---|\n| 1 |",
        }
        lines = _process(item, sub=sub)
        # Locate the title row and the row containing the table top.
        title_idx = next(
            i for i, l in enumerate(lines)
            if "<strong>Title here</strong>" in str(l.get("html", "")))
        top_idx = next(
            i for i, l in enumerate(lines)
            if "┌" in str(l.get("html", "")))
        # Exactly one empty row between title and table top border.
        assert top_idx - title_idx == 2
        assert str(lines[title_idx + 1].get("html", "")) == ""


class TestCombinedDecorations:
    """Realistic combinations from the NFS / ANFS source data."""

    def test_atx_heading_then_after_label_banner(self):
        # The &8000 case in ANFS: ATX heading (BEFORE_LABEL) above the
        # `.rom_header`/`.language_entry` labels, then an AFTER_LABEL
        # banner BETWEEN labels and the data row. The ATX heading
        # renders as a section-break card (chapter-style); the
        # AFTER_LABEL banner renders as code-style block comment rows
        # (title + blank + description) so it reads as part of the
        # surrounding listing flow.
        item = {
            "addr": 0x8000, "type": "byte", "values": [0x00],
            "labels": ["language_entry", "rom_header"],
            "comments_before_label": ["# ANFS ROM disassembly"],
            "comment_inline": "no-language sentinel",
        }
        sub = {
            "addr": 0x8000, "title": "Sideways ROM header",
            "description": "MOS dispatches JMP &8000 ...",
            "align": "after_label",
        }
        lines = _process(item, sub=sub)
        roles = [_classify(l) for l in lines]
        # empty, ATX heading (banner), label, label,
        # title (comment), blank (empty), description (comment), data
        assert roles == [
            "empty", "banner", "label", "label",
            "comment", "empty", "comment", "data",
        ]
        # Only the ATX heading row carries banner=True / section_break;
        # the AFTER_LABEL banner is regular comment rows.
        banner_rows = [l for l in lines if l.get("banner")]
        assert len(banner_rows) == 1
        assert banner_rows[0].get("section_break") is True
