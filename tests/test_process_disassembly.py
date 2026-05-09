"""End-to-end integration test for `process_disassembly`.

Feeds a hand-crafted JSON payload through the top-level entry point
and asserts on the resulting section / line structure. The payload
covers the integration of `_process_item` with `_align_inline_comments`
and `_split_into_sections`, which the focused unit tests don't.

Tests assert against the structured output rather than rendered HTML
so they're resilient to template / CSS changes.
"""

from __future__ import annotations

from generator.disassembly import process_disassembly


def _payload():
    """A minimal but representative JSON payload.

    Layout mirrors what the NFS / ANFS sources emit under dasmos 1.5:

        &8000  language_entry  EQUB &00       (sub-header card AFTER label)
        &8001                  EQUB &42, &43  (rom_header_byte1 padding)
        &8003  service_entry   JMP service_handler
        &8006  rom_type        EQUB %10000010 ; ROM type   (format_hint binary)

    Plus an ATX heading in comments_before_label of the very first
    item, exercising the section-break flag.
    """
    return {
        "meta": {"load_addr": 0x8000, "end_addr": 0x8009},
        "constants": {},
        "subroutines": [],
        "banners": [
            {"addr": 0x8000, "title": "Sideways ROM header — language slot",
             "description": "MOS dispatches JMP &8000.",
             "align": "after_label"},
            {"addr": 0x8003, "title": "Service entry slot",
             "description": "MOS dispatches service-call here.",
             "align": "after_label"},
            {"addr": 0x8006, "title": "ROM type byte",
             "description": "| Bit | Value |\n|-----|-------|\n| 7 | 1 |",
             "align": "after_label"},
        ],
        "external_labels": {},
        "memory_map": [],
        "items": [
            {
                "addr": 0x8000, "bytes": [0x00],
                "type": "byte", "values": [0x00],
                "labels": ["language_entry", "rom_header"],
                "comments_before_label": ["# ANFS disassembly"],
                "comment_inline": "no-language sentinel",
            },
            {
                "addr": 0x8001, "bytes": [0x42, 0x43],
                "type": "byte", "values": [0x42, 0x43],
                "labels": ["rom_header_byte1"],
                "comment_inline": "unused padding",
            },
            {
                "addr": 0x8003, "bytes": [0x4C, 0x54, 0x8A],
                "type": "code", "mnemonic": "jmp",
                "operand": "service_handler",
                "target": 0x8A54, "target_label": "service_handler",
                "labels": ["service_entry"],
            },
            {
                "addr": 0x8006, "bytes": [0x82],
                "type": "byte", "values": [0x82],
                "labels": ["rom_type"],
                "format_hints": ["binary"],
                "comment_inline": "ROM type",
            },
        ],
    }


class TestProcessDisassembly:

    def test_returns_sections(self):
        sections = process_disassembly(_payload(), version_id="x")
        assert sections
        assert all("lines" in s and "has_binary_addr" in s for s in sections)

    def test_atx_heading_starts_a_section(self):
        sections = process_disassembly(_payload(), version_id="x")
        # The leading _empty_line() lands in its own tiny pre-section
        # (the section split happens at the ATX heading row, putting
        # the empty separator with the previous content). The ATX
        # heading itself is the FIRST line of the section that follows.
        for s in sections:
            if s["lines"] and s["lines"][0].get("banner"):
                assert "<h2>" in str(s["lines"][0]["html"])
                assert "ANFS disassembly" in str(s["lines"][0]["html"])
                return
        raise AssertionError("No section starts with the ATX heading row")

    def test_after_label_banners_dont_split_section(self):
        sections = process_disassembly(_payload(), version_id="x")
        # Each banner card with after_label alignment should sit in the
        # SAME section as its labels and data, not in a section of its
        # own. Find the section containing the rom_type banner card and
        # confirm it also contains the .rom_type label and the EQUB row.
        for s in sections:
            html_blob = "".join(str(l.get("html", "")) for l in s["lines"])
            if "ROM type byte" in html_blob:
                assert ".rom_type" in html_blob
                assert "EQUB" in html_blob
                assert "%10000010" in html_blob
                return
        raise AssertionError("Did not find a section with the rom_type banner")

    def test_format_hint_binary_visible_in_data_row(self):
        sections = process_disassembly(_payload(), version_id="x")
        all_html = "".join(
            str(l.get("html", ""))
            for s in sections for l in s["lines"]
        )
        assert "%10000010" in all_html
        # The default-hex form of 0x82 should not appear as the
        # primary display of the rom_type byte (it's still in the
        # tooltip via data-tip="...").
        # The hex 0x42 / 0x43 padding bytes should still render as hex
        # since they have no format_hint.
        assert "&amp;42" in all_html
        assert "&amp;43" in all_html

    def test_labels_appear_in_order(self):
        sections = process_disassembly(_payload(), version_id="x")
        all_html = "\n".join(
            str(l.get("html", ""))
            for s in sections for l in s["lines"]
        )
        # `.language_entry` and `.rom_header` are both at &8000;
        # they should appear in the same order as in `item.labels`.
        i_le = all_html.find(".language_entry")
        i_rh = all_html.find(".rom_header")
        assert 0 <= i_le < i_rh

    def test_section_split_is_only_on_section_break(self):
        sections = process_disassembly(_payload(), version_id="x")
        # Only one section_break in the payload (the ATX heading), so
        # we get exactly two sections: the pre-ATX preamble (an empty
        # line that the renderer emits before BEFORE_LABEL decoration)
        # and the main content section. The three after_label banner
        # cards do NOT split sections — they sit alongside their items.
        assert len(sections) == 2
        # The pre-section is just the leading empty line.
        assert all(
            str(l.get("html", "")) == "" for l in sections[0]["lines"]
        )
        # All four items end up in the second section.
        body_html = "".join(
            str(l.get("html", "")) for l in sections[1]["lines"]
        )
        for label in (".language_entry", ".rom_header", ".service_entry",
                      ".rom_type"):
            assert label in body_html
