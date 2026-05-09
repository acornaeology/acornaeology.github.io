"""Unit tests for small helpers in `generator.disassembly`.

ATX heading detection, comment filtering, comment-segment splitting,
format-hint formatting, and section splitting. These are fast,
hermetic tests that don't touch Jinja or the filesystem.
"""

from __future__ import annotations

import pytest
from markupsafe import Markup

from generator.disassembly import (
    _atx_heading_match,
    _split_atx_headings,
    _filter_comments,
    _split_comment_segments,
    _format_byte_value,
    _format_word_value,
    _hinted_value_width,
    _split_into_sections,
)


class TestAtxHeadingMatch:

    def test_h1(self):
        assert _atx_heading_match("# Title") == (1, "Title")

    def test_h2_through_h6(self):
        for n in range(2, 7):
            text = "#" * n + " Heading"
            assert _atx_heading_match(text) == (n, "Heading")

    def test_seven_hashes_is_not_a_heading(self):
        assert _atx_heading_match("####### Title") is None

    def test_no_hash_is_not_a_heading(self):
        assert _atx_heading_match("Title") is None

    def test_hash_without_space_is_not_a_heading(self):
        assert _atx_heading_match("#Title") is None

    def test_optional_closing_hashes_stripped(self):
        assert _atx_heading_match("## Title ##") == (2, "Title")

    def test_leading_whitespace_tolerated(self):
        assert _atx_heading_match("   # Title") == (1, "Title")

    def test_trailing_whitespace_stripped(self):
        assert _atx_heading_match("# Title   ") == (1, "Title")

    def test_multiline_text_is_not_a_heading(self):
        assert _atx_heading_match("# Title\nbody") is None

    def test_empty_string(self):
        assert _atx_heading_match("") is None

    def test_inline_hash_not_a_heading(self):
        assert _atx_heading_match("Use # for headings") is None


class TestSplitAtxHeadings:

    def test_partitions_headings_and_prose(self):
        comments = ["# Intro", "Just prose", "## Sub"]
        headings, regular = _split_atx_headings(comments)
        assert headings == [(1, "Intro"), (2, "Sub")]
        assert regular == ["Just prose"]

    def test_all_prose(self):
        headings, regular = _split_atx_headings(["a", "b"])
        assert headings == []
        assert regular == ["a", "b"]

    def test_all_headings(self):
        headings, regular = _split_atx_headings(["# A", "## B"])
        assert headings == [(1, "A"), (2, "B")]
        assert regular == []

    def test_empty(self):
        assert _split_atx_headings([]) == ([], [])


class TestFilterComments:

    def test_drops_xref_summary_text(self):
        # Old shape: cross-references lived in comments_before. dasmos
        # 1.5+ moves them to xref_summaries which we don't read; the
        # filter is the legacy-source path.
        comments = ["&8000 referenced 1 time by &04E6", "Real comment"]
        assert _filter_comments(comments, sub=None) == ["Real comment"]

    def test_drops_asterisk_separator(self):
        comments = ["**********", "Real comment"]
        assert _filter_comments(comments, sub=None) == ["Real comment"]

    def test_keeps_short_runs_of_asterisks(self):
        # `*` and `**` and `***` aren't separators -- they're emphasis
        # in Markdown source. Only runs of 4+ are treated as decoration.
        assert _filter_comments(["***"], sub=None) == ["***"]
        assert _filter_comments(["****"], sub=None) == []

    def test_drops_banner_body_when_sub_has_title(self):
        sub = {"title": "ROM type byte"}
        comments = ["ROM type byte\n\nBit decoding...", "Other"]
        assert _filter_comments(comments, sub) == ["Other"]

    def test_keeps_banner_body_when_no_title_match(self):
        sub = {"title": "Different title"}
        comments = ["ROM type byte\n\nBit decoding..."]
        assert _filter_comments(comments, sub) == ["ROM type byte\n\nBit decoding..."]

    def test_no_sub_no_filter(self):
        comments = ["Anything goes"]
        assert _filter_comments(comments, sub=None) == ["Anything goes"]


class TestSplitCommentSegments:

    def test_no_table_single_prose_segment(self):
        text = "Just a paragraph"
        assert _split_comment_segments(text) == [("prose", "Just a paragraph")]

    def test_pipe_table_alone(self):
        text = "| A | B |\n|---|---|\n| 1 | 2 |"
        segments = _split_comment_segments(text)
        assert len(segments) == 1
        assert segments[0][0] == "table"

    def test_prose_then_table(self):
        text = "Intro:\n\n| A | B |\n|---|---|\n| 1 | 2 |"
        segments = _split_comment_segments(text)
        assert len(segments) == 2
        assert segments[0][0] == "prose"
        assert segments[0][1].startswith("Intro:")
        assert segments[1][0] == "table"

    def test_table_then_prose(self):
        text = "| A |\n|---|\n| 1 |\n\nFollowing prose"
        segments = _split_comment_segments(text)
        assert len(segments) == 2
        assert segments[0][0] == "table"
        assert segments[1][0] == "prose"
        assert "Following prose" in segments[1][1]

    def test_two_tables(self):
        text = "| A |\n|---|\n| 1 |\n\n| B |\n|---|\n| 2 |"
        segments = _split_comment_segments(text)
        kinds = [k for k, _ in segments]
        assert kinds.count("table") == 2

    def test_empty_text(self):
        assert _split_comment_segments("") == [("prose", "")]


class TestFormatByteValue:

    def test_default_hex(self):
        assert _format_byte_value(0x82, None) == "&82"
        assert _format_byte_value(0x82, "hex") == "&82"

    def test_binary(self):
        assert _format_byte_value(0x82, "binary") == "%10000010"

    def test_decimal(self):
        assert _format_byte_value(130, "decimal") == "130"

    def test_char_printable(self):
        assert _format_byte_value(ord("A"), "char") == "'A'"

    def test_char_falls_back_to_hex_for_nonprintable(self):
        assert _format_byte_value(0, "char") == "&00"

    def test_char_skips_quote_and_backslash(self):
        # ' and \ would need escaping inside 'X' literal -- fall back
        # to hex rather than emit ambiguous source.
        assert _format_byte_value(ord("'"), "char") == "&27"
        assert _format_byte_value(ord("\\"), "char") == "&5C"

    def test_inkey_signed(self):
        # BBC INKEY parameter: 0..127 unsigned, 128..255 -> -128..-1
        assert _format_byte_value(0, "inkey") == "0"
        assert _format_byte_value(127, "inkey") == "127"
        assert _format_byte_value(128, "inkey") == "-128"
        assert _format_byte_value(255, "inkey") == "-1"

    def test_octal(self):
        assert _format_byte_value(8, "octal") == "&O10"

    def test_unknown_hint_falls_back_to_hex(self):
        assert _format_byte_value(0x42, "marshmallow") == "&42"


class TestFormatWordValue:

    def test_default_hex(self):
        assert _format_word_value(0x8000, None) == "&8000"

    def test_binary_16_bits(self):
        assert _format_word_value(0x8000, "binary") == "%1000000000000000"

    def test_decimal(self):
        assert _format_word_value(32768, "decimal") == "32768"


class TestHintedValueWidth:

    def test_no_hints_returns_default_byte_width(self):
        item = {"type": "byte", "values": [0x42]}
        assert _hinted_value_width(item) == 3

    def test_no_hints_returns_default_word_width(self):
        item = {"type": "word", "values": [0x4242]}
        assert _hinted_value_width(item) == 5

    def test_binary_widens_byte_to_9(self):
        item = {"type": "byte", "values": [0x82], "format_hints": ["binary"]}
        assert _hinted_value_width(item) == 9

    def test_takes_max_across_hints(self):
        # Mixed hints -- the widest one (binary, 9) wins for layout.
        item = {
            "type": "byte",
            "values": [1, 2, 3],
            "format_hints": ["decimal", "binary", "hex"],
        }
        assert _hinted_value_width(item) == 9

    def test_null_hints_dont_widen(self):
        item = {"type": "byte", "values": [1, 2], "format_hints": [None, None]}
        assert _hinted_value_width(item) == 3


class TestSplitIntoSections:

    def _line(self, **kw):
        # Helper: construct a minimal line dict.
        base = {"id": None, "addr": None, "html": Markup("")}
        base.update(kw)
        return base

    def test_section_break_starts_new_section(self):
        lines = [
            self._line(html=Markup("first")),
            self._line(banner=True, section_break=True, html=Markup("banner")),
            self._line(html=Markup("after")),
        ]
        sections = _split_into_sections(lines)
        assert len(sections) == 2
        assert len(sections[0]["lines"]) == 1
        assert len(sections[1]["lines"]) == 2

    def test_banner_without_section_break_keeps_section(self):
        lines = [
            self._line(html=Markup("first")),
            self._line(banner=True, html=Markup("after-label banner")),
            self._line(html=Markup("data")),
        ]
        sections = _split_into_sections(lines)
        assert len(sections) == 1
        assert len(sections[0]["lines"]) == 3

    def test_no_break_at_start_keeps_single_section(self):
        # An item that begins with a section_break should not produce
        # a leading empty section.
        lines = [
            self._line(banner=True, section_break=True, html=Markup("banner")),
            self._line(html=Markup("data")),
        ]
        sections = _split_into_sections(lines)
        assert len(sections) == 1

    def test_has_binary_addr_flag(self):
        lines = [
            self._line(banner=True, section_break=True, html=Markup("banner")),
            self._line(addr="8000", binary_addr="C000", html=Markup("data")),
        ]
        [section] = _split_into_sections(lines)
        assert section["has_binary_addr"] is True

    def test_no_binary_addr_when_absent(self):
        lines = [
            self._line(addr="8000", html=Markup("data")),
        ]
        [section] = _split_into_sections(lines)
        assert section["has_binary_addr"] is False
