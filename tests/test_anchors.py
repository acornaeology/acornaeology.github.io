"""Tests for listing-row anchor naming and resolution (`generator.anchors`)
and its use by the cross-page `address:` link resolver."""

from generator.anchors import (
    AnchorIndex,
    anchor_id,
    nearest_at_or_before,
    row_anchor_id,
    stored_anchor_id,
)
from generator.build import _filter_subroutines, apply_address_uri_links


class TestAnchorNames:
    def test_anchor_id_is_four_digit_upper_hex(self):
        assert anchor_id(0x0400) == "addr-0400"
        assert anchor_id(0xE0ab) == "addr-E0AB"

    def test_row_anchor_id_uses_run_address(self):
        assert row_anchor_id({"addr": 0x0400, "binary_addr": 0x51B0}) == "addr-0400"

    def test_stored_anchor_id_uses_binary_address(self):
        assert stored_anchor_id({"addr": 0x0400, "binary_addr": 0x51B0}) == "addr-51B0"

    def test_stored_anchor_id_is_none_when_not_relocated(self):
        assert stored_anchor_id({"addr": 0x8000}) is None


class TestNearestAtOrBefore:
    def test_exact(self):
        assert nearest_at_or_before([0x10, 0x20], 0x20) == 0x20

    def test_inside_item(self):
        assert nearest_at_or_before([0x10, 0x20], 0x1F) == 0x10

    def test_before_first(self):
        assert nearest_at_or_before([0x10, 0x20], 0x0F) is None

    def test_empty(self):
        assert nearest_at_or_before([], 0x10) is None


class TestAnchorIndex:
    def _index(self):
        return AnchorIndex.from_items([
            {"addr": 0x8000},
            {"addr": 0x8003},
            {"addr": 0x0400, "binary_addr": 0x8010},
        ])

    def test_resolves_exact_run_address(self):
        assert self._index().resolve(0x8003) == "addr-8003"

    def test_resolves_stored_address(self):
        assert self._index().resolve(0x8010) == "addr-8010"

    def test_resolves_relocated_run_address(self):
        assert self._index().resolve(0x0400) == "addr-0400"

    def test_resolves_mid_item_to_preceding_anchor(self):
        assert self._index().resolve(0x8001) == "addr-8000"

    def test_beyond_last_resolves_to_last(self):
        assert self._index().resolve(0x9000) == "addr-8010"

    def test_before_first_is_none(self):
        assert self._index().resolve(0x0100) is None


class TestApplyAddressUriLinks:
    def _anchors(self):
        return {"1": AnchorIndex.from_items([{"addr": 0x8000}, {"addr": 0x8003}])}

    def test_links_to_listing_anchor(self):
        html = '<a href="address:8003">go</a>'
        out = apply_address_uri_links(html, self._anchors(), default_version="1")
        assert out == '<a href="1.html#addr-8003">go</a>'

    def test_mid_item_resolves_to_preceding_anchor(self):
        html = '<a href="address:8001@1">go</a>'
        out = apply_address_uri_links(html, self._anchors())
        assert out == '<a href="1.html#addr-8000">go</a>'

    def test_hex_flag_appends_second_link(self):
        html = '<a href="address:8003?hex">go</a>'
        out = apply_address_uri_links(html, self._anchors(), default_version="1")
        assert out == ('<a href="1.html#addr-8003">go</a> '
                       '(<a href="1.html#addr-8003"><code>&amp;8003</code></a>)')

    def test_memory_map_entry_wins(self):
        html = '<a href="address:0070">zp</a>'
        out = apply_address_uri_links(
            html, self._anchors(), default_version="1",
            version_mm_links={"1": {0x70: "1-memory-map.html#mm-zp"}})
        assert out == ('<a class="mm-link" target="memory-map" '
                       'href="1-memory-map.html#mm-zp">zp</a>')

    def test_before_first_anchor_is_left_unchanged(self, capsys):
        html = '<a href="address:0100">go</a>'
        out = apply_address_uri_links(html, self._anchors(), default_version="1")
        assert out == html
        assert "no anchor" in capsys.readouterr().out

    def test_unknown_version_is_left_unchanged(self, capsys):
        html = '<a href="address:8000@9">go</a>'
        out = apply_address_uri_links(html, self._anchors())
        assert out == html
        assert "unknown version" in capsys.readouterr().out


class TestSubroutineNavAnchors:
    def test_each_entry_carries_its_row_anchor(self):
        data = {
            "meta": {"load_addr": 0x8000, "end_addr": 0xC000},
            "subroutines": [{"addr": 0x8003, "name": "b"}],
            "banners": [{"addr": 0x8000, "name": "a"}],
        }
        assert [(s["name"], s["anchor"]) for s in _filter_subroutines(data)] == [
            ("a", "addr-8000"), ("b", "addr-8003")]
