"""Tests for listing-row anchor naming and resolution (`generator.anchors`)
and its use by the link resolvers and the listing renderer."""

import pytest

from generator.anchors import (
    AnchorIndex,
    LinkFlags,
    anchor_id,
    move_anchor_id,
    nearest_at_or_before,
    parse_link_flags,
    stored_anchor_id,
)
from generator.build import (
    _filter_subroutines,
    apply_address_uri_links,
    apply_label_uri_links,
)
from generator.disassembly import process_disassembly


# Two overlays of &0400 (the Dunjunz disc's keyboard and joystick images)
# beside unmoved code at &3000.
OVERLAYS = {
    "meta": {
        "schema_version": 4, "load_addr": 0x3000, "end_addr": 0x6000,
        "moves": [
            {"name": "low_keyboard", "dest": 0x0400, "src": 0x51B0, "length": 4},
            {"name": "low_joystick", "dest": 0x0400, "src": 0x55B0, "length": 4},
        ],
    },
    "items": [
        {"addr": 0x3000, "bytes": [0x20, 0x00, 0x04], "type": "code",
         "mnemonic": "jsr", "operand": "add_points", "target": 0x0400,
         "target_label": "add_points"},
        {"addr": 0x0400, "binary_addr": 0x51B0, "move": "low_keyboard",
         "bytes": [0xEA], "type": "code", "mnemonic": "nop",
         "labels": ["add_points"],
         "references": [{"addr": 0x3000, "kind": "direct"}]},
        {"addr": 0x0401, "binary_addr": 0x51B1, "move": "low_keyboard",
         "bytes": [0x4C, 0x00, 0x04], "type": "code", "mnemonic": "jmp",
         "operand": "add_points", "target": 0x0400,
         "target_label": "add_points", "target_move": "low_keyboard"},
        {"addr": 0x0400, "binary_addr": 0x55B0, "move": "low_joystick",
         "bytes": [0xEA], "type": "code", "mnemonic": "nop",
         "labels": ["add_points_joystick"],
         "references": [{"addr": 0x0401, "kind": "direct",
                         "move": "low_joystick"}]},
        {"addr": 0x0401, "binary_addr": 0x55B1, "move": "low_joystick",
         "bytes": [0x4C, 0x00, 0x04], "type": "code", "mnemonic": "jmp",
         "operand": "add_points_joystick", "target": 0x0400,
         "target_label": "add_points_joystick", "target_move": "low_joystick"},
    ],
    "subroutines": [
        {"addr": 0x0400, "binary_addr": 0x51B0, "move": "low_keyboard",
         "name": "add_points"},
        {"addr": 0x0400, "binary_addr": 0x55B0, "move": "low_joystick",
         "name": "add_points_joystick"},
    ],
}

# Ole's crack: the image stored at &2400 runs at &0400, so a stored
# address (&2420) equals a run address in the same move.
OVERLAPPING = {
    "meta": {"moves": [
        {"name": "game_image", "dest": 0x0400, "src": 0x2400, "length": 0x40},
    ]},
    "items": [
        {"addr": 0x0420, "binary_addr": 0x2420, "move": "game_image"},
        {"addr": 0x2420, "binary_addr": 0x4420, "move": "game_image"},
    ],
}


class TestAnchorNames:
    def test_plain(self):
        assert anchor_id(0xE0ab) == "addr-E0AB"

    def test_move_qualified(self):
        assert move_anchor_id(0x0400, "low_joystick") == "addr-0400@low_joystick"

    def test_stored(self):
        assert stored_anchor_id(0x51B0) == "stored-51B0"


class TestNearestAtOrBefore:
    def test_exact(self):
        assert nearest_at_or_before([0x10, 0x20], 0x20) == 0x20

    def test_inside_item(self):
        assert nearest_at_or_before([0x10, 0x20], 0x1F) == 0x10

    def test_before_first(self):
        assert nearest_at_or_before([0x10, 0x20], 0x0F) is None


class TestLinkFlags:
    def test_hex_and_move(self):
        flags = parse_link_flags("move=low_joystick&hex")
        assert flags.hex and flags.move == "low_joystick" and not flags.error

    def test_html_escaped_separator(self):
        flags = parse_link_flags("stored&amp;HEX")
        assert flags.stored and flags.hex

    def test_unknown_flag(self):
        assert "unknown flag" in parse_link_flags("bogus").error

    def test_two_copy_selectors(self):
        assert "at most one" in parse_link_flags("stored&unmoved").error

    def test_round_trip(self):
        assert str(LinkFlags(move="m", hex=True)) == "move=m&hex"


class TestPermalinksWithoutCollisions:
    """Listings without overlapping copies keep exactly the old ids."""

    def test_relocated_row_keeps_plain_ids(self):
        index = AnchorIndex([{"addr": 0x0400, "binary_addr": 0x8010}])
        assert index.row_ids({"addr": 0x0400, "binary_addr": 0x8010}) == (
            "addr-0400", [])
        assert index.stored_ids({"addr": 0x0400, "binary_addr": 0x8010}) == (
            "addr-8010", [])

    def test_unrelocated_row_has_no_stored_id(self):
        index = AnchorIndex([{"addr": 0x8000}])
        assert index.stored_ids({"addr": 0x8000}) == (None, [])

    def test_named_move_without_collision_keeps_plain_id_plus_aliases(self):
        item = {"addr": 0x0400, "binary_addr": 0x8010, "move": "m"}
        index = AnchorIndex([item], ["m"])
        assert index.row_ids(item) == ("addr-0400", ["addr-0400@m"])
        assert index.stored_ids(item) == ("addr-8010", ["stored-8010"])


class TestOverlays:
    def setup_method(self):
        self.index = AnchorIndex.from_data(OVERLAYS)
        self.keyboard, self.joystick = OVERLAYS["items"][1], OVERLAYS["items"][3]

    def test_first_move_keeps_plain_id(self):
        assert self.index.row_ids(self.keyboard) == (
            "addr-0400", ["addr-0400@low_keyboard"])

    def test_later_move_is_qualified(self):
        assert self.index.row_ids(self.joystick) == ("addr-0400@low_joystick", [])

    def test_stored_ids_are_plain_when_unique(self):
        assert self.index.stored_ids(self.joystick) == (
            "addr-55B0", ["stored-55B0"])

    def test_no_unresolved_duplicates(self):
        assert self.index.unresolved_duplicates == []

    def test_default_resolution_is_ambiguous(self):
        assert self.index.resolve(0x0400).id == "addr-0400"
        assert self.index.resolve(0x0400).ambiguous

    def test_move_flag_selects_copy(self):
        resolution = self.index.resolve(0x0402, LinkFlags(move="low_joystick"))
        assert resolution.id == "addr-0401@low_joystick"
        assert not resolution.ambiguous

    def test_stored_flag(self):
        assert self.index.resolve(0x55B1, LinkFlags(stored=True)).id == "addr-55B1"

    def test_label_rows(self):
        assert self.index.label_link_id("add_points_joystick") == (
            "addr-0400@low_joystick")
        assert self.index.label_flags("add_points_joystick").move == "low_joystick"


class TestOverlappingStoredAndRunAddresses:
    def test_run_address_wins_plain_id_and_stored_is_qualified(self):
        index = AnchorIndex.from_data(OVERLAPPING)
        low, high = OVERLAPPING["items"]
        assert index.row_ids(high)[0] == "addr-2420"
        assert index.stored_ids(low) == ("stored-2420", [])


class TestLegacyJsonWithoutMoves:
    def test_collisions_left_as_before_and_reported(self):
        items = [{"addr": 0x0400, "binary_addr": 0x51B0},
                 {"addr": 0x0400, "binary_addr": 0x55B0}]
        index = AnchorIndex(items)
        assert [index.row_ids(i)[0] for i in items] == ["addr-0400", "addr-0400"]
        assert index.unresolved_duplicates == ["addr-0400"]


class TestApplyAddressUriLinks:
    def _anchors(self):
        return {"1": AnchorIndex.from_addrs([0x8000, 0x8003])}

    def test_links_to_listing_anchor(self):
        out = apply_address_uri_links(
            '<a href="address:8003">go</a>', self._anchors(), default_version="1")
        assert out == '<a href="1.html#addr-8003">go</a>'

    def test_mid_item_resolves_to_preceding_anchor(self):
        out = apply_address_uri_links(
            '<a href="address:8001@1">go</a>', self._anchors())
        assert out == '<a href="1.html#addr-8000">go</a>'

    def test_hex_flag_appends_second_link(self):
        out = apply_address_uri_links(
            '<a href="address:8003?hex">go</a>', self._anchors(),
            default_version="1")
        assert out == ('<a href="1.html#addr-8003">go</a> '
                       '(<a href="1.html#addr-8003"><code>&amp;8003</code></a>)')

    def test_memory_map_entry_wins(self):
        out = apply_address_uri_links(
            '<a href="address:0070">zp</a>', self._anchors(), default_version="1",
            version_mm_links={"1": {0x70: "1-memory-map.html#mm-zp"}})
        assert out == ('<a class="mm-link" target="memory-map" '
                       'href="1-memory-map.html#mm-zp">zp</a>')

    def test_before_first_anchor_is_left_unchanged(self, capsys):
        html = '<a href="address:0100">go</a>'
        assert apply_address_uri_links(
            html, self._anchors(), default_version="1") == html
        assert "no anchor" in capsys.readouterr().out

    def test_unknown_flag_is_left_unchanged(self, capsys):
        html = '<a href="address:8000?bogus">go</a>'
        assert apply_address_uri_links(
            html, self._anchors(), default_version="1") == html
        assert "unknown flag" in capsys.readouterr().out

    def test_move_flag_links_to_that_copy(self, capsys):
        out = apply_address_uri_links(
            '<a href="address:0400?move=low_joystick&amp;hex">j</a>',
            {"disc": AnchorIndex.from_data(OVERLAYS)}, default_version="disc")
        assert out.startswith('<a href="disc.html#addr-0400@low_joystick">j</a> (')
        assert capsys.readouterr().out == ""

    def test_ambiguous_address_warns(self, capsys):
        out = apply_address_uri_links(
            '<a href="address:0400">k</a>',
            {"disc": AnchorIndex.from_data(OVERLAYS)}, default_version="disc")
        assert out == '<a href="disc.html#addr-0400">k</a>'
        assert "several copies" in capsys.readouterr().out

    def test_unknown_move_warns(self, capsys):
        html = '<a href="address:0400?move=nope">k</a>'
        assert apply_address_uri_links(
            html, {"disc": AnchorIndex.from_data(OVERLAYS)},
            default_version="disc") == html
        assert "unknown move" in capsys.readouterr().out


class TestApplyLabelUriLinks:
    def test_label_in_a_move_pins_its_copy(self, capsys):
        anchors = {"disc": AnchorIndex.from_data(OVERLAYS)}
        html = apply_label_uri_links(
            '<a href="label:add_points_joystick?hex">j</a>',
            {"disc": {"add_points_joystick": 0x0400}}, default_version="disc",
            version_anchors=anchors)
        assert html == '<a href="address:0400?move=low_joystick&hex">j</a>'
        out = apply_address_uri_links(html, anchors, default_version="disc")
        assert out.startswith('<a href="disc.html#addr-0400@low_joystick">')
        assert capsys.readouterr().out == ""


class TestSubroutineNavAnchors:
    def test_entries_carry_their_row_anchor(self):
        data = {
            "meta": {"load_addr": 0x8000, "end_addr": 0xC000},
            "items": [{"addr": 0x8000}, {"addr": 0x8003}],
            "subroutines": [{"addr": 0x8003, "name": "b"}],
            "banners": [{"addr": 0x8000, "name": "a"}],
        }
        anchors = AnchorIndex.from_data(data)
        assert [(s["name"], s["anchor"])
                for s in _filter_subroutines(data, anchors)] == [
            ("a", "addr-8000"), ("b", "addr-8003")]

    def test_entry_point_inside_an_instruction_links_to_its_row(self):
        # osword_12_handler in NFS 3.35D enters at &8E7C, the operand of
        # the 3-byte instruction at &8E7A (a BIT-skip entry).
        data = {
            "meta": {"load_addr": 0x8000, "end_addr": 0xC000},
            "items": [{"addr": 0x8E7A}, {"addr": 0x8E7D}],
            "subroutines": [{"addr": 0x8E7C, "name": "osword_12_handler"}],
        }
        anchors = AnchorIndex.from_data(data)
        assert _filter_subroutines(data, anchors)[0]["anchor"] == "addr-8E7A"

    def test_entry_at_a_stored_address_keeps_its_working_link(self):
        # ADFS 1.30's nmi_write_code is listed at its stored address,
        # which a relocated row already answers to.
        data = {
            "meta": {"load_addr": 0x8000, "end_addr": 0xC000},
            "items": [{"addr": 0x0D0A, "binary_addr": 0xBCDF}],
            "subroutines": [{"addr": 0xBCDF, "name": "nmi_write_code"}],
        }
        anchors = AnchorIndex.from_data(data)
        assert _filter_subroutines(data, anchors)[0]["anchor"] == "addr-BCDF"

    def test_overlay_subroutines_link_to_their_own_copy(self):
        data = dict(OVERLAYS, meta=dict(OVERLAYS["meta"], load_addr=0, end_addr=0x10000))
        anchors = AnchorIndex.from_data(data)
        assert [s["anchor"] for s in _filter_subroutines(data, anchors)] == [
            "addr-0400", "addr-0400@low_joystick"]


class TestListingRendering:
    @pytest.fixture
    def html(self):
        sections = process_disassembly(OVERLAYS, version_id="disc")
        return "".join(str(line["html"]) + repr(line)
                       for section in sections for line in section["lines"])

    def test_rows_carry_unique_ids(self):
        sections = process_disassembly(OVERLAYS, version_id="disc")
        ids = [line["id"] for section in sections for line in section["lines"]
               if line.get("id")]
        assert len(ids) == len(set(ids))
        assert "addr-0400@low_joystick" in ids

    def test_operand_links_to_target_move(self, html):
        assert 'href="#addr-0400@low_joystick"' in html

    def test_come_from_links_to_callers_copy(self, html):
        assert 'href="#addr-0401@low_joystick">← 0401 JMP' in html
