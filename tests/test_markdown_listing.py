"""Unit tests for `generator.markdown_listing.render_markdown`.

Focus: author Markdown must never emit raw HTML into the listing.
Command-reference comments routinely contain angle-bracket syntax
notation like "<Title>" or "<List Spec>"; CommonMark would otherwise
pass these through as live tags. An unclosed "<Title>" in particular
flips the browser into RAWTEXT parsing and hides the rest of the page
(see the &9E46 truncation report).
"""

from __future__ import annotations

from generator.markdown_listing import render_markdown


def _render(text, *, inline=False):
    return str(render_markdown(text, valid_addrs=set(), sorted_addrs=[],
                               inline=inline))


class TestRawHtmlIsEscaped:

    def test_title_token_is_not_emitted_as_a_tag(self):
        # The exact token that broke the published 1.30 listing.
        html = _render("7: <Title>")
        assert "<title>" not in html.lower()
        assert "&lt;Title&gt;" in html

    def test_angle_bracket_syntax_tokens_render_literally(self):
        html = _render("Params: <List Spec> (L)(W)(R)(E)")
        assert "<list spec>" not in html.lower()
        assert "&lt;List Spec&gt;" in html

    def test_block_level_html_line_is_escaped(self):
        html = _render("<div>not markup</div>")
        assert "<div>" not in html.lower()
        assert "&lt;div&gt;" in html

    def test_inline_context_also_escapes(self):
        html = _render("see <Title> here", inline=True)
        assert "<title>" not in html.lower()
        assert "&lt;Title&gt;" in html

    def test_ordinary_markdown_still_renders(self):
        # Emphasis and code spans must keep working -- we only neutralise
        # raw HTML, not the Markdown constructs the author relies on.
        html = _render("a *b* and `c`")
        assert "<em>b</em>" in html
        assert "<code>c</code>" in html


class TestLabelAndGlossaryLinks:
    """label:NAME and glossary:TERM resolution in listing comments."""

    def test_label_resolves_to_rom_anchor(self):
        html = str(render_markdown(
            "see [print_cmos_pair](label:print_cmos_pair)",
            valid_addrs={0x9668}, sorted_addrs=[0x9668],
            label_addrs={"print_cmos_pair": 0x9668}))
        assert 'href="#addr-9668"' in html
        assert ">print_cmos_pair</a>" in html

    def test_label_hex_flag_appends_address(self):
        html = str(render_markdown(
            "[print_cmos_pair](label:print_cmos_pair?hex)",
            valid_addrs={0x9668}, sorted_addrs=[0x9668],
            label_addrs={"print_cmos_pair": 0x9668}))
        assert "&amp;9668" in html
        assert html.count('href="#addr-9668"') == 2

    def test_label_prefers_memory_map_link(self):
        html = str(render_markdown(
            "[net_frame_flags](label:net_frame_flags)",
            valid_addrs=set(), sorted_addrs=[],
            mm_links={0x0D3E: "4.24-memory-map.html#mm-net_frame_flags"},
            label_addrs={"net_frame_flags": 0x0D3E}))
        assert 'class="mm-link" target="memory-map"' in html
        assert "4.24-memory-map.html#mm-net_frame_flags" in html

    def test_unknown_label_left_as_target(self):
        html = str(render_markdown(
            "[nope](label:nope)", valid_addrs=set(), sorted_addrs=[],
            label_addrs={}))
        assert 'href="label:nope"' in html

    def test_glossary_link_emits_ref_anchor_case_insensitive(self):
        # Author writes the anchor slug; case-insensitive (CMOS -> cmos).
        html = str(render_markdown(
            "the [CMOS](glossary:CMOS) clock",
            valid_addrs=set(), sorted_addrs=[],
            glossary_lookup={"cmos": {"slug": "cmos",
                                      "tooltip": "CMOS RAM: battery-backed."}}))
        assert 'href="glossary.html#term-cmos"' in html
        assert 'class="glossary-ref"' in html
        assert 'data-tip="CMOS RAM: battery-backed."' in html
        assert ">CMOS</a>" in html

    def test_glossary_multiword_slug(self):
        html = str(render_markdown(
            "[Master 128](glossary:master-128)",
            valid_addrs=set(), sorted_addrs=[],
            glossary_lookup={"master-128": {"slug": "master-128",
                                            "tooltip": "The BBC Master 128."}}))
        assert 'href="glossary.html#term-master-128"' in html
        assert ">Master 128</a>" in html

    def test_unknown_glossary_slug_left_as_target(self):
        html = str(render_markdown(
            "[Nope](glossary:nope)", valid_addrs=set(), sorted_addrs=[],
            glossary_lookup={}))
        assert 'href="glossary:nope"' in html
