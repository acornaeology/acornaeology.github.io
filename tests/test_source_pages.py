"""Unit tests for the multi-source companion-page helpers in
`generator.build`: version-metadata resolution, source-view page
naming, the repo-file → site-page map, and the link rewriter that
repoints GitHub-relative source links at their rendered pages.
"""

from __future__ import annotations

from generator.build import (
    _resolve_version_meta_filepath,
    _source_output_filename,
    _build_source_page_map,
    _rewrite_source_file_links,
)


class TestResolveVersionMetaFilepath:

    def test_prefers_binary_layout_when_present(self, tmp_path):
        (tmp_path / "binary").mkdir()
        binary_meta = tmp_path / "binary" / "binary.json"
        binary_meta.write_text("{}")
        assert _resolve_version_meta_filepath(tmp_path) == binary_meta

    def test_falls_back_to_rom_layout(self, tmp_path):
        # No binary/binary.json — the returned path is the rom one
        # (whether or not it exists, matching the callers' .exists() guard).
        assert _resolve_version_meta_filepath(tmp_path) == \
            tmp_path / "rom" / "rom.json"


class TestSourceOutputFilename:

    def test_uses_explicit_slug(self):
        entry = {"path": "output/x-driver-a.asm", "slug": "driver-a"}
        assert _source_output_filename("joystik", entry) == "joystik-driver-a.html"

    def test_defaults_to_path_stem_lowercased(self):
        entry = {"path": "basic/Keypad.BAS"}
        assert _source_output_filename("keypad", entry) == "keypad-keypad.html"


class TestBuildSourcePageMap:

    def _make_version(self, tmp_path):
        version_dirpath = tmp_path / "versions" / "proj-keypad"
        (version_dirpath / "basic").mkdir(parents=True)
        (version_dirpath / "output").mkdir()
        (version_dirpath / "binary").mkdir()
        (version_dirpath / "basic" / "keypad.bas").write_text("10 REM")
        (version_dirpath / "output" / "keypad.asm").write_text("; asm")
        (version_dirpath / "output" / "keypad.json").write_text("{}")
        (version_dirpath / "binary" / "KEYPAD").write_bytes(b"\x00")
        (version_dirpath / "binary" / "binary.json").write_text("{}")
        rom_meta = {
            "source_files": [
                {"label": "BASIC", "slug": "basic", "path": "basic/keypad.bas"},
            ]
        }
        return version_dirpath, rom_meta

    def test_source_file_maps_to_its_own_page(self, tmp_path):
        version_dirpath, rom_meta = self._make_version(tmp_path)
        page_map = _build_source_page_map([("keypad", version_dirpath, rom_meta)])
        bas = (version_dirpath / "basic" / "keypad.bas").resolve()
        assert page_map[bas] == "keypad-basic.html"

    def test_primary_artefacts_map_to_disassembly_page(self, tmp_path):
        version_dirpath, rom_meta = self._make_version(tmp_path)
        page_map = _build_source_page_map([("keypad", version_dirpath, rom_meta)])
        asm = (version_dirpath / "output" / "keypad.asm").resolve()
        binary = (version_dirpath / "binary" / "KEYPAD").resolve()
        assert page_map[asm] == "keypad.html"
        assert page_map[binary] == "keypad.html"

    def test_binary_metadata_json_is_not_mapped(self, tmp_path):
        version_dirpath, rom_meta = self._make_version(tmp_path)
        page_map = _build_source_page_map([("keypad", version_dirpath, rom_meta)])
        meta = (version_dirpath / "binary" / "binary.json").resolve()
        assert meta not in page_map

    def test_source_file_wins_over_primary_alias(self, tmp_path):
        # A driver .asm listed as a source_file gets its own page, not the
        # disassembly-page alias its output/ location would otherwise give.
        version_dirpath = tmp_path / "versions" / "proj-joystik"
        (version_dirpath / "output").mkdir(parents=True)
        (version_dirpath / "output" / "j-driver-a.asm").write_text("; a")
        rom_meta = {"source_files": [
            {"label": "A", "slug": "driver-a", "path": "output/j-driver-a.asm"}]}
        page_map = _build_source_page_map([("joystik", version_dirpath, rom_meta)])
        asm = (version_dirpath / "output" / "j-driver-a.asm").resolve()
        assert page_map[asm] == "joystik-driver-a.html"


class TestRewriteSourceFileLinks:

    def test_repoints_known_source_file(self, tmp_path):
        doc_dirpath = tmp_path / "docs" / "analysis"
        doc_dirpath.mkdir(parents=True)
        bas = (tmp_path / "versions" / "v" / "basic" / "k.bas")
        bas.parent.mkdir(parents=True)
        bas.write_text("10 REM")
        page_map = {bas.resolve(): "keypad-basic.html"}
        html = '<a href="../../versions/v/basic/k.bas">the BASIC</a>'
        out = _rewrite_source_file_links(html, doc_dirpath, page_map)
        assert out == '<a href="keypad-basic.html">the BASIC</a>'

    def test_preserves_fragment(self, tmp_path):
        doc_dirpath = tmp_path
        target = tmp_path / "x.asm"
        target.write_text("; asm")
        page_map = {target.resolve(): "v.html"}
        html = '<a href="x.asm#L20">line 20</a>'
        out = _rewrite_source_file_links(html, doc_dirpath, page_map)
        assert out == '<a href="v.html#L20">line 20</a>'

    def test_leaves_unknown_and_external_links_untouched(self, tmp_path):
        page_map = {(tmp_path / "known.bas").resolve(): "p.html"}
        html = ('<a href="https://example.com/known.bas">x</a>'
                '<a href="other.txt">y</a>'
                '<a href="#anchor">z</a>')
        assert _rewrite_source_file_links(html, tmp_path, page_map) == html

    def test_empty_map_is_a_noop(self, tmp_path):
        html = '<a href="x.bas">x</a>'
        assert _rewrite_source_file_links(html, tmp_path, {}) == html
