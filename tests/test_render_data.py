"""Tests for the byte / word / fill / string renderers.

These exercise the data-emission pipeline for byte and word items,
including the dasmos 1.4 `format_hints` parallel array and the
`expressions` parallel array used for symbolic operands.
"""

from __future__ import annotations

import re

from generator.disassembly import (
    ExprContext,
    _render_bytes,
    _render_words,
    _render_string,
    _render_fill,
    _split_string_parts,
    build_macros,
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


def _v3_ctx(valid_addrs=(), label_tooltips=None, mm_links=None,
            macro_names=()):
    """An ExprContext for schema v3 with the given linking lookups."""
    return ExprContext(
        schema_version=3,
        valid_addrs=set(valid_addrs),
        label_tooltips=dict(label_tooltips or {}),
        mm_links=dict(mm_links or {}),
        macro_names=set(macro_names),
    )


class TestRenderV3Expressions:
    """Schema v3: `expressions[i]` is a `{"text", "tree"}` object, not a
    bare string. The renderer must use `text` for display (never leak a
    dict repr) and linkify names its `tree` resolves."""

    def test_object_expression_renders_text_not_repr(self):
        item = {
            "type": "byte", "values": [0x19],
            "expressions": [
                {"text": "copyright - language_entry",
                 "tree": {"op": "sub",
                          "left": {"sym": "copyright"},
                          "right": {"sym": "language_entry"}}}],
        }
        out = str(_render_bytes(item, expr_ctx=_v3_ctx()))
        assert "copyright - language_entry" in out
        assert "{'text'" not in out and "'tree'" not in out

    def test_word_object_expression_renders_text(self):
        item = {
            "type": "word", "values": [0x8000],
            "expressions": [{"text": "start_addr", "tree": {"sym": "start_addr"}}],
        }
        out = str(_render_words(item, expr_ctx=_v3_ctx()))
        assert "start_addr" in out
        assert "{'text'" not in out

    def test_ref_node_links_to_anchor(self):
        item = {
            "type": "byte", "values": [0x78],
            "expressions": [
                {"text": "<(fn_openin)",
                 "tree": {"op": "lowbyte",
                          "operand": {"group": {
                              "ref": 0xBF78, "name": "fn_openin"}}}}],
        }
        ctx = _v3_ctx(valid_addrs={0xBF78})
        out = str(_render_bytes(item, expr_ctx=ctx))
        assert '<a href="#addr-BF78"' in out
        assert ">fn_openin</a>" in out

    def test_macro_call_links_to_definition(self):
        item = {
            "type": "byte", "values": [0x4B],
            "expressions": [
                {"text": 'pack_lo("BRK")',
                 "tree": {"macro_call": "pack_lo", "args": [{"str": "BRK"}]}}],
        }
        ctx = _v3_ctx(macro_names={"pack_lo"})
        out = str(_render_bytes(item, expr_ctx=ctx))
        assert 'class="macro-link" href="#macro-pack_lo"' in out
        assert ">pack_lo</a>" in out
        # The beebasm-flavoured text (parens, arg) is preserved verbatim.
        assert "(&#34;BRK&#34;)" in out or '("BRK")' in _visible(out)

    def test_unknown_macro_not_linked(self):
        # A macro_call with no matching definition degrades to plain text.
        item = {
            "type": "byte", "values": [0x00],
            "expressions": [
                {"text": "mystery(1)",
                 "tree": {"macro_call": "mystery", "args": [{"int": 1}]}}],
        }
        out = str(_render_bytes(item, expr_ctx=_v3_ctx()))
        assert "macro-link" not in out
        assert "mystery(1)" in _visible(out)

    def test_unknown_node_kind_degrades_to_text(self):
        # A node kind the renderer doesn't recognise must not crash or
        # leak — fall back to the sibling `text`.
        item = {
            "type": "byte", "values": [0x00],
            "expressions": [
                {"text": "future_thing", "tree": {"brand_new_kind": 42}}],
        }
        out = str(_render_bytes(item, expr_ctx=_v3_ctx()))
        assert "future_thing" in out
        assert "brand_new_kind" not in out

    def test_width_uses_text_length(self):
        # Grouping/wrapping must measure the display text, not the object.
        item = {
            "type": "byte",
            "values": [1, 2],
            "expressions": [
                {"text": "aaaaaaaa", "tree": {"sym": "aaaaaaaa"}},
                {"text": "bbbbbbbb", "tree": {"sym": "bbbbbbbb"}}],
        }
        out = str(_render_bytes(item, max_width=64, expr_ctx=_v3_ctx()))
        assert "aaaaaaaa" in out and "bbbbbbbb" in out

    def test_v2_string_expression_unaffected(self):
        # Without an expr_ctx (or with a v2 one), a bare-string
        # expression renders exactly as before.
        item = {
            "type": "byte", "values": [0x19],
            "expressions": ["copyright - rom_header"],
        }
        out = str(_render_bytes(item))
        assert "copyright - rom_header" in out
        assert "{'text'" not in out


class TestBuildMacros:

    def _v3_doc(self):
        return {
            "meta": {"schema_version": 3},
            "macros": [
                {"name": "pack_lo", "params": ["mnem"], "emit": "byte",
                 "body": {"text": "(mnem[0] AND &1f) AND &ff", "tree": {}}}],
        }

    def test_v2_has_no_macros_block(self):
        assert build_macros({"meta": {"schema_version": 2}, "macros": []}) == []

    def test_absent_schema_version_treated_as_v2(self):
        assert build_macros({"meta": {}}) == []

    def test_v3_builds_macro_view(self):
        macros = build_macros(self._v3_doc())
        assert len(macros) == 1
        m = macros[0]
        assert m["name"] == "pack_lo"
        assert m["id"] == "macro-pack_lo"
        assert m["params"] == ["mnem"]
        assert m["emit"] == "byte"
        # Body text is escaped (`&` → `&amp;`) for safe HTML embedding.
        assert "&amp;1f" in str(m["body"])

    def test_v3_with_no_macros_is_empty(self):
        assert build_macros({"meta": {"schema_version": 3}, "macros": []}) == []


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


class TestSplitStringParts:

    def test_all_printable(self):
        bytes_ = list(b"hello")
        assert _split_string_parts(bytes_) == [("string", "hello")]

    def test_trailing_nul(self):
        bytes_ = list(b"Line Jammed") + [0]
        assert _split_string_parts(bytes_) == [
            ("string", "Line Jammed"),
            ("byte", 0),
        ]

    def test_leading_nul(self):
        bytes_ = [0] + list(b"hello")
        assert _split_string_parts(bytes_) == [
            ("byte", 0),
            ("string", "hello"),
        ]

    def test_embedded_nul(self):
        bytes_ = list(b"abc") + [0] + list(b"def")
        assert _split_string_parts(bytes_) == [
            ("string", "abc"),
            ("byte", 0),
            ("string", "def"),
        ]

    def test_quote_byte_split_out(self):
        # `"` (0x22) inside a quoted run would break beebasm syntax;
        # emit it as &22 between two quoted runs instead.
        bytes_ = list(b'a"b')
        assert _split_string_parts(bytes_) == [
            ("string", "a"),
            ("byte", 0x22),
            ("string", "b"),
        ]

    def test_all_non_printable(self):
        bytes_ = [0, 13, 0xFF]
        result = _split_string_parts(bytes_)
        assert result == [("byte", 0), ("byte", 13), ("byte", 0xFF)]

    def test_high_bit_byte(self):
        # 0x80..0xFF are non-printable in 7-bit ASCII.
        bytes_ = list(b"err") + [0xA0]
        assert _split_string_parts(bytes_) == [
            ("string", "err"),
            ("byte", 0xA0),
        ]

    def test_empty(self):
        assert _split_string_parts([]) == []


class TestRenderString:

    def test_basic_string_no_bytes_falls_back(self):
        # Defensive path when bytes[] is absent.
        item = {"string": "(C)ROFF"}
        out = str(_render_string(item))
        assert "EQUS" in out
        assert "(C)ROFF" in out

    def test_basic_string_with_bytes(self):
        item = {"string": "hello", "bytes": list(b"hello")}
        out = str(_render_string(item))
        assert "EQUS" in out
        assert ">&quot;hello&quot;<" in out
        # No trailing byte marker for an all-printable string.
        assert "&amp;" not in out or out.count("&amp;") == 0

    def test_trailing_nul_renders_as_byte(self):
        # The reproducer from issue #13: NUL terminator should appear
        # outside the quoted run as `, &00`, not collapsed into `.`.
        item = {
            "string": "Line Jammed.",  # disassembler-substituted text
            "bytes": list(b"Line Jammed") + [0],
        }
        out = str(_render_string(item))
        assert ">&quot;Line Jammed&quot;<" in out
        assert "&amp;00" in out
        # The misleading "." inside the quotes from the JSON `string`
        # field must NOT appear in the rendered output.
        assert "Line Jammed.</span>" not in out

    def test_embedded_quote_split_out(self):
        item = {"string": 'a.b', "bytes": list(b'a"b')}
        out = str(_render_string(item))
        assert ">&quot;a&quot;<" in out
        assert "&amp;22" in out
        assert ">&quot;b&quot;<" in out

    def test_byte_carries_tooltip(self):
        item = {"string": "x.", "bytes": [ord("x"), 0]}
        out = str(_render_string(item))
        # data-tip on the &00 byte includes decimal/hex/binary forms.
        assert "data-tip=" in out
        assert "%00000000" in out


class TestRenderFill:

    def test_basic_fill(self):
        item = {"value": 0x00, "length": 16}
        out = str(_render_fill(item))
        assert "FILL" in out
        assert "16" in out
        assert "&amp;00" in out
