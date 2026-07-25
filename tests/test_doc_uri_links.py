"""Unit tests for the doc-level label:/glossary: URI resolvers in build.py."""
from generator.build import apply_label_uri_links, apply_glossary_uri_links


class TestLabelUriLinks:
    def test_label_translates_to_address_uri(self):
        html = '<a href="label:print_cmos_pair">print_cmos_pair</a>'
        out = apply_label_uri_links(
            html, {"4.24": {"print_cmos_pair": 0x9668}},
            default_version="4.24")
        assert '<a href="address:9668">print_cmos_pair</a>' == out

    def test_label_preserves_version_and_flag(self):
        html = '<a href="label:foo@3.60?hex">foo</a>'
        out = apply_label_uri_links(
            html, {"3.60": {"foo": 0xE263}}, default_version="4.24")
        assert '<a href="address:E263@3.60?hex">foo</a>' == out

    def test_unknown_label_left_intact(self):
        html = '<a href="label:nope">nope</a>'
        out = apply_label_uri_links(html, {"4.24": {}}, default_version="4.24")
        assert out == html


class TestGlossaryUriLinks:
    LOOKUP = {"cmos": {"slug": "cmos", "tooltip": "CMOS RAM: battery-backed."}}

    def test_glossary_rewrites_to_ref_anchor(self):
        html = 'the <a href="glossary:cmos">CMOS</a> clock'
        out = apply_glossary_uri_links(html, self.LOOKUP)
        assert 'href="glossary.html#term-cmos"' in out
        assert 'class="glossary-ref"' in out
        assert 'data-tip="CMOS RAM: battery-backed."' in out
        assert ">CMOS</a>" in out

    def test_glossary_case_insensitive(self):
        html = '<a href="glossary:CMOS">CMOS</a>'
        out = apply_glossary_uri_links(html, self.LOOKUP)
        assert 'href="glossary.html#term-cmos"' in out

    def test_unknown_slug_left_intact(self):
        html = '<a href="glossary:nope">x</a>'
        out = apply_glossary_uri_links(html, self.LOOKUP)
        assert out == html
