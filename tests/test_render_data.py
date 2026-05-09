"""Tests for the byte / word / fill / string renderers.

These exercise the data-emission pipeline for byte and word items,
including the dasmos 1.4 `format_hints` parallel array and the
`expressions` parallel array used for symbolic operands.
"""

from __future__ import annotations

import re

from generator.disassembly import (
    _render_bytes,
    _render_words,
    _render_string,
    _render_fill,
)


def _visible(html):
    """Strip HTML tags and decode entities to inspect the rendered text."""
    text = re.sub(r"<[^>]+>", "", str(html))
    return (text
            .replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", '"')
            .replace("&#x27;", "'")
            .replace("&#39;", "'"))


class TestRenderBytes:

    def test_single_byte_default_hex(self):
        item = {"type": "byte", "values": [0x42]}
        out = str(_render_bytes(item))
        assert "EQUB" in out
        assert "&amp;42" in out

    def test_format_hint_binary(self):
        item = {"type": "byte", "values": [0x82], "format_hints": ["binary"]}
        out = str(_render_bytes(item))
        assert "%10000010" in out
        # Default hex form should NOT appear in display (still in tooltip)
        # The data-tip carries it; the rendered text should not.
        assert _visible(out).split()[-1] == "%10000010"

    def test_format_hint_decimal(self):
        item = {"type": "byte", "values": [130], "format_hints": ["decimal"]}
        out = str(_render_bytes(item))
        assert _visible(out).split()[-1] == "130"

    def test_tooltip_carries_all_forms(self):
        item = {"type": "byte", "values": [0x82], "format_hints": ["binary"]}
        out = str(_render_bytes(item))
        # Tooltip text follows the data-tip="..." attribute
        m = re.search(r'data-tip="([^"]+)"', out)
        assert m is not None
        tip = m.group(1)
        assert "130" in tip
        assert "&82" in tip.replace("&amp;", "&")
        assert "%10000010" in tip

    def test_mixed_format_hints(self):
        item = {
            "type": "byte",
            "values": [0x01, 0x02, 0x03],
            "format_hints": ["decimal", "binary", None],
        }
        visible = _visible(_render_bytes(item))
        # Order matters; commas separate the rendered values
        assert "1," in visible
        assert "%00000010" in visible
        assert "&03" in visible

    def test_expression_renders_instead_of_value(self):
        item = {
            "type": "byte",
            "values": [0x19],
            "expressions": ["copyright - rom_header"],
        }
        out = str(_render_bytes(item))
        assert "copyright - rom_header" in out
        # data-tip still shows the resolved value
        m = re.search(r'data-tip="([^"]+)"', out)
        assert m is not None
        assert "25" in m.group(1) or "&19" in m.group(1).replace("&amp;", "&")

    def test_multi_byte_grouped_within_max_width(self):
        item = {"type": "byte", "values": [1, 2, 3, 4]}
        out = str(_render_bytes(item, max_width=64))
        # All four values should appear, separated by ", "
        visible = _visible(out)
        for v in ("&01", "&02", "&03", "&04"):
            assert v in visible

    def test_wraps_when_too_many_values(self):
        # 30 bytes at &XX width 3 + ", " = 5 each → wraps several times
        item = {"type": "byte", "values": list(range(30))}
        out = str(_render_bytes(item, max_width=64))
        # Multi-line output uses "\n" inside the rendered Markup
        assert "\n" in str(out)


class TestRenderWords:

    def test_single_word_default_hex(self):
        item = {"type": "word", "values": [0x8000]}
        out = str(_render_words(item))
        assert "EQUW" in out
        assert "&amp;8000" in out

    def test_format_hint_binary_renders_16_bits(self):
        item = {"type": "word", "values": [0x8000], "format_hints": ["binary"]}
        visible = _visible(_render_words(item))
        assert "%1000000000000000" in visible

    def test_expression_renders(self):
        item = {
            "type": "word",
            "values": [0x8000],
            "expressions": ["start_addr"],
        }
        out = str(_render_words(item))
        assert "start_addr" in out


class TestRenderString:

    def test_basic_string(self):
        item = {"string": "(C)ROFF"}
        out = str(_render_string(item))
        assert "EQUS" in out
        assert "(C)ROFF" in out

    def test_quotes_escaped(self):
        item = {"string": 'a"b'}
        out = str(_render_string(item))
        assert "&quot;" in out


class TestRenderFill:

    def test_basic_fill(self):
        item = {"value": 0x00, "length": 16}
        out = str(_render_fill(item))
        assert "FILL" in out
        assert "16" in out
        assert "&amp;00" in out
