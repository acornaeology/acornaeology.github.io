"""Tests for GitHub-style heading ids on rendered documents."""

from generator.build import _render_document_markdown, _rewrite_md_links_to_html
from generator.headings import add_heading_ids, heading_slug


class TestHeadingSlug:
    def test_plain(self):
        assert heading_slug("The HFE captures") == "the-hfe-captures"

    def test_punctuation_dropped(self):
        assert heading_slug('What "binding to an lvalue parameter" actually does') == (
            "what-binding-to-an-lvalue-parameter-actually-does")

    def test_each_space_becomes_a_hyphen(self):
        # An em dash is dropped, leaving its two spaces.
        assert heading_slug("1. The ordinary case — a named scalar") == (
            "1-the-ordinary-case--a-named-scalar")

    def test_hyphen_and_underscore_kept(self):
        assert heading_slug("rx_frame-b") == "rx_frame-b"

    def test_non_ascii_letters_kept(self):
        assert heading_slug("Ölfass Größe") == "ölfass-größe"


class TestAddHeadingIds:
    def test_inline_markup_contributes_its_text(self):
        html = "<h3>2. An element — <code>m(i,j)</code></h3>"
        assert add_heading_ids(html) == (
            '<h3 id="2-an-element--mij">2. An element — <code>m(i,j)</code></h3>')

    def test_repeats_are_numbered(self):
        out = add_heading_ids("<h2>Notes</h2><h2>Notes</h2><h3>Notes</h3>")
        assert out == ('<h2 id="notes">Notes</h2><h2 id="notes-1">Notes</h2>'
                       '<h3 id="notes-2">Notes</h3>')

    def test_existing_id_kept(self):
        html = '<h2 id="custom">Notes</h2>'
        assert add_heading_ids(html) == html

    def test_entities_unescaped_before_slugging(self):
        assert add_heading_ids("<h2>R&amp;D</h2>") == '<h2 id="rd">R&amp;D</h2>'

    def test_empty_slug_gets_no_id(self):
        assert add_heading_ids("<h2>!!!</h2>") == "<h2>!!!</h2>"


class TestDocumentRendering:
    def test_same_page_heading_link_resolves(self):
        html = _render_document_markdown(
            "See [the captures](#the-hfe-captures).\n\n### The HFE captures\n")
        assert 'href="#the-hfe-captures"' in html
        assert '<h3 id="the-hfe-captures">' in html


class TestCrossAnalysisLinks:
    ANALYSES = [{"url": "docs/analysis/findings.md"}]

    def test_fragment_kept(self):
        html = '<a href="findings.md#the-level-5-reset">x</a>'
        assert _rewrite_md_links_to_html(html, self.ANALYSES) == (
            '<a href="findings.html#the-level-5-reset">x</a>')

    def test_without_fragment(self):
        html = '<a href="../analysis/findings.md">x</a>'
        assert _rewrite_md_links_to_html(html, self.ANALYSES) == (
            '<a href="findings.html">x</a>')
