"""Tests for listing keys and links between a project's listings
(`generator.listings`)."""

from pathlib import Path

from generator.anchors import AnchorIndex
from generator.build import (
    _index_version_entry,
    _Listing,
    _VersionPlan,
    apply_address_uri_links,
    apply_label_uri_links,
)
from generator.listings import (
    ListingLinks,
    listing_key,
    listing_page_stem,
    listing_version,
    resolve_listing_key,
)
from generator.markdown_listing import listing_context, render_markdown


class TestListingKeys:
    def test_primary_key_is_the_version(self):
        assert listing_key("disc_master") == "disc_master"

    def test_extra_key_names_its_slug(self):
        assert listing_key("disc_master", "editor") == "disc_master/editor"

    def test_page_stems_match_the_published_pages(self):
        assert listing_page_stem("disc_master") == "disc_master"
        assert listing_page_stem("joystik/driver-a") == "joystik-driver-a"

    def test_version_of_a_key(self):
        assert listing_version("disc_master/editor") == "disc_master"
        assert listing_version("disc_master") == "disc_master"


class TestResolveListingKey:
    def test_no_qualifier_is_the_default(self):
        assert resolve_listing_key(None, "disc") == "disc"
        assert resolve_listing_key("", "disc") == "disc"

    def test_full_key(self):
        assert resolve_listing_key("disc_master/editor", "disc") == (
            "disc_master/editor")

    def test_slug_relative_to_the_default_version(self):
        assert resolve_listing_key("/editor", "disc_master") == (
            "disc_master/editor")
        assert resolve_listing_key("/editor", "disc_master/loader") == (
            "disc_master/editor")

    def test_slash_alone_is_the_version_primary(self):
        assert resolve_listing_key("/", "disc_master/editor") == "disc_master"

    def test_relative_qualifier_needs_a_default(self):
        assert resolve_listing_key("/editor", None) is None


def _disc_links():
    """The game at &2000-&2FFF and, as its own listing, an editor whose
    rows share those addresses."""
    return ListingLinks(
        anchors={
            "disc_master": AnchorIndex.from_addrs([0x2000, 0x23B2]),
            "disc_master/editor": AnchorIndex.from_addrs([0x1910, 0x23B2]),
        },
        labels={
            "disc_master": {"game_entry": 0x23B2},
            "disc_master/editor": {"editor_entry": 0x23B2},
        },
        mm_links={"disc_master/editor": {
            0x0070: "disc_master-editor-memory-map.html#mm-ed_ptr"}},
    )


class TestLinksBetweenListings:
    def test_address_in_another_listing(self, capsys):
        out = apply_address_uri_links(
            '<a href="address:23B2@disc_master/editor">e</a>', _disc_links())
        assert out == '<a href="disc_master-editor.html#addr-23B2">e</a>'
        assert capsys.readouterr().out == ""

    def test_same_address_in_the_primary(self):
        out = apply_address_uri_links(
            '<a href="address:23B2@disc_master">g</a>', _disc_links())
        assert out == '<a href="disc_master.html#addr-23B2">g</a>'

    def test_relative_to_the_doc_version(self):
        out = apply_address_uri_links(
            '<a href="address:23B2@/editor">e</a>', _disc_links(),
            default_listing="disc_master")
        assert out == '<a href="disc_master-editor.html#addr-23B2">e</a>'

    def test_memory_map_of_another_listing(self):
        out = apply_address_uri_links(
            '<a href="address:0070@disc_master/editor">p</a>', _disc_links())
        assert out == ('<a class="mm-link" target="memory-map" '
                       'href="disc_master-editor-memory-map.html#mm-ed_ptr">'
                       'p</a>')

    def test_unknown_listing_is_left_unchanged(self, capsys):
        html = '<a href="address:23B2@disc_master/nope">x</a>'
        assert apply_address_uri_links(html, _disc_links()) == html
        assert "unknown version or listing" in capsys.readouterr().out

    def test_relative_link_without_a_default_warns(self, capsys):
        html = '<a href="address:23B2@/editor">x</a>'
        assert apply_address_uri_links(html, _disc_links()) == html
        assert "names no listing" in capsys.readouterr().out

    def test_label_in_another_listing(self):
        links = _disc_links()
        html = apply_label_uri_links(
            '<a href="label:editor_entry@/editor">e</a>', links,
            default_listing="disc_master")
        assert html == '<a href="address:23B2@/editor">e</a>'
        out = apply_address_uri_links(html, links,
                                      default_listing="disc_master")
        assert out == '<a href="disc_master-editor.html#addr-23B2">e</a>'


class TestListingCommentLinks:
    def _render(self, text, key="disc_master"):
        links = _disc_links()
        with listing_context(links.labels[key], {}, links.anchors[key],
                             links=links, listing_key=key):
            return str(render_markdown(text, None, None, inline=True))

    def test_unqualified_stays_on_the_page(self):
        assert 'href="#addr-23B2"' in self._render("[g](address:23B2)")

    def test_own_listing_named_stays_on_the_page(self):
        assert 'href="#addr-23B2"' in self._render(
            "[g](address:23B2@disc_master)")

    def test_another_listing(self):
        assert 'href="disc_master-editor.html#addr-23B2"' in self._render(
            "[e](address:23B2@/editor)")

    def test_back_to_the_primary(self):
        assert 'href="disc_master.html#addr-23B2"' in self._render(
            "[g](address:23B2@/)", key="disc_master/editor")

    def test_label_in_another_listing(self):
        assert 'href="disc_master-editor.html#addr-23B2"' in self._render(
            "[e](label:editor_entry@/editor)")

    def test_unknown_listing_warns(self, capsys):
        out = self._render("[x](address:23B2@/nope)")
        assert 'href="address:23B2"' in out
        assert "unknown listing" in capsys.readouterr().out


def _plan(rom_meta):
    data = {"meta": {}, "items": [{"addr": 0x2000}]}
    primary = _Listing("disc_master", None, rom_meta.get("label", "Disc"),
                       data, AnchorIndex.from_data(data))
    editor = _Listing("disc_master/editor", "editor", "*EDITOR", data,
                      AnchorIndex.from_data(data))
    return _VersionPlan("disc_master", Path("."), Path("binary.json"),
                        rom_meta, "Disc", primary, [editor])


class TestPeerListingsOnTheIndex:
    def test_parts_sit_under_their_program(self):
        entry = _index_version_entry(_plan({"title": "Disc"}))
        assert not entry["peers"]
        assert [r["label"] for r in entry["additional_disassemblies"]] == [
            "*EDITOR"]

    def test_peers_include_the_primary(self):
        entry = _index_version_entry(_plan({"title": "Disc",
                                            "label": "The game"}))
        assert entry["peers"]
        assert [(r["label"], r["disassembly_url"])
                for r in entry["additional_disassemblies"]] == [
            ("The game", "disc_master.html"),
            ("*EDITOR", "disc_master-editor.html")]
