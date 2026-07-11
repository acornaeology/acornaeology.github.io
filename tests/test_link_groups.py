"""Tests for the disassembly-page link categorisation / grouping.

`_link_category` resolves each link to a category (explicit `category`
in rom.json, else inferred from its icon) and `_group_links` buckets a
flat link list into the ordered `LINK_CATEGORIES` groups, omitting empty
ones. The grouping is what makes research-source attribution explicit on
the page (our artefacts vs the ROM's provenance vs sources consulted vs
discussion), so it's worth locking down.
"""

from __future__ import annotations

from generator.build import (
    DEFAULT_LINK_CATEGORY,
    LINK_CATEGORIES,
    _build_source_link,
    _dedup_links,
    _group_links,
    _link_category,
    _merge_references,
)


class TestLinkCategory:

    def test_explicit_category_wins_over_icon(self):
        # An author marking a referenced disassembly as further reading
        # overrides the icon-inferred "source".
        link = {"icon": "ref", "category": "related"}
        assert _link_category(link) == "related"

    def test_icon_inference_when_no_category(self):
        assert _link_category({"icon": "github"}) == "ours"
        assert _link_category({"icon": "map"}) == "ours"
        assert _link_category({"icon": "chip"}) == "rom-image"
        assert _link_category({"icon": "ref"}) == "source"
        assert _link_category({"icon": "doc"}) == "source"
        assert _link_category({"icon": "chat"}) == "discussion"
        assert _link_category({"icon": "bug"}) == "feedback"

    def test_unknown_icon_falls_back_to_default(self):
        assert _link_category({"icon": "sparkle"}) == DEFAULT_LINK_CATEGORY
        assert _link_category({}) == DEFAULT_LINK_CATEGORY

    def test_unknown_explicit_category_coerced_to_default(self):
        # A typo'd category surfaces as a visible group rather than a
        # dropped link.
        assert _link_category({"category": "typo"}) == DEFAULT_LINK_CATEGORY


class TestGroupLinks:

    def _links(self):
        return [
            {"label": "src", "icon": "github"},
            {"label": "mm", "icon": "map"},
            {"label": "rom", "icon": "chip"},
            {"label": "harston", "icon": "ref"},
            {"label": "guide", "icon": "doc"},
            {"label": "forum", "icon": "chat"},
            {"label": "issue", "icon": "bug"},
        ]

    def test_groups_are_in_category_order(self):
        groups = _group_links(self._links())
        order = [g["category"] for g in groups]
        expected = [key for key, _ in LINK_CATEGORIES
                    if key in order]
        assert order == expected
        assert order == ["ours", "rom-image", "source",
                         "discussion", "feedback"]

    def test_empty_groups_are_omitted(self):
        # No "related" link → no Further reading group.
        groups = _group_links(self._links())
        assert "related" not in [g["category"] for g in groups]

    def test_headings_match_category_spec(self):
        headings = dict(LINK_CATEGORIES)
        for group in _group_links(self._links()):
            assert group["heading"] == headings[group["category"]]

    def test_links_keep_order_within_group(self):
        links = [
            {"label": "first", "icon": "ref"},
            {"label": "second", "icon": "doc"},
            {"label": "third", "icon": "ref"},
        ]
        [source_group] = _group_links(links)
        assert [l["label"] for l in source_group["links"]] == [
            "first", "second", "third"]

    def test_two_links_one_group(self):
        groups = _group_links([
            {"label": "a", "icon": "github"},
            {"label": "b", "icon": "map"},
        ])
        assert len(groups) == 1
        assert groups[0]["category"] == "ours"
        assert len(groups[0]["links"]) == 2

    def test_no_links_no_groups(self):
        assert _group_links([]) == []


class TestBuildSourceLink:

    def test_sources_become_variants(self):
        rom_meta = {"sources": [
            {"assembler": "beebasm", "url": "u/a.asm"},
            {"assembler": "64tass", "url": "u/a.s"},
        ]}
        link = _build_source_link(rom_meta, "repo")
        assert link["category"] == "ours"
        assert link["icon"] == "github"
        assert [(v["label"], v["url"]) for v in link["variants"]] == [
            ("beebasm", "u/a.asm"), ("64tass", "u/a.s")]

    def test_variant_falls_back_to_source_label(self):
        link = _build_source_link({"sources": [{"url": "u/a.asm"}]}, "repo")
        assert link["variants"][0]["label"] == "source"

    def test_legacy_github_link_used_when_no_sources(self):
        rom_meta = {"links": [
            {"label": "Disassembly source on GitHub",
             "url": "u/a.asm", "icon": "github"}]}
        link = _build_source_link(rom_meta, "repo")
        assert "variants" not in link
        assert link["url"] == "u/a.asm"
        assert link["category"] == "ours"

    def test_injected_repo_link_when_nothing_supplied(self):
        link = _build_source_link({}, "https://example/repo")
        assert link["url"] == "https://example/repo"
        assert link["icon"] == "github"
        assert link["category"] == "ours"


class TestMergeReferences:

    def _shared(self):
        return [
            {"id": "mos", "label": "Model-B MOS", "url": "u/mos-b"},
            {"label": "Tech description", "url": "u/tech"},
        ]

    def test_no_version_refs_returns_shared(self):
        assert _merge_references(self._shared(), []) == self._shared()

    def test_version_ref_specialises_by_id_in_place(self):
        version = [{"id": "mos", "label": "Master MOS", "url": "u/mos-m"}]
        merged = _merge_references(self._shared(), version)
        # Same position, replaced content.
        assert merged[0]["label"] == "Master MOS"
        assert merged[0]["url"] == "u/mos-m"
        assert [r["url"] for r in merged] == ["u/mos-m", "u/tech"]

    def test_version_ref_without_id_enriches(self):
        version = [{"label": "Extra", "url": "u/extra"}]
        merged = _merge_references(self._shared(), version)
        assert [r["url"] for r in merged] == ["u/mos-b", "u/tech", "u/extra"]

    def test_new_id_enriches_not_overrides(self):
        version = [{"id": "other", "label": "Other", "url": "u/other"}]
        merged = _merge_references(self._shared(), version)
        assert len(merged) == 3
        assert merged[-1]["url"] == "u/other"

    def test_suppress_removes_shared_without_replacement(self):
        version = [{"id": "mos", "suppress": True}]
        merged = _merge_references(self._shared(), version)
        assert [r["url"] for r in merged] == ["u/tech"]

    def test_shared_list_not_mutated(self):
        shared = self._shared()
        _merge_references(shared, [{"id": "mos", "label": "X", "url": "u/x"}])
        assert shared[0]["label"] == "Model-B MOS"


class TestDedupLinks:

    def test_drops_later_duplicate_urls(self):
        out = _dedup_links([
            {"label": "a", "url": "u/1"},
            {"label": "b", "url": "u/2"},
            {"label": "a again", "url": "u/1"},
        ])
        assert [l["label"] for l in out] == ["a", "b"]

    def test_links_without_url_all_kept(self):
        out = _dedup_links([{"label": "x"}, {"label": "y"}])
        assert len(out) == 2
