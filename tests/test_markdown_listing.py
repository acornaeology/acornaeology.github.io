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
