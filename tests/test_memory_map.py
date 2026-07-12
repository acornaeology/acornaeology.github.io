"""Unit tests for memory-map data normalisation in `generator.build`.

`_normalized_memory_map` reconciles the two dasmos JSON schema shapes
into one v4-shaped list so a single rendering path serves them both:

- v4 (`meta.schema_version >= 4`): `access` is already an orthogonal
  `["r", "w", "b"]` subset and indexing bases are folded into
  `memory_map` as `["b"]` rows — passed through unchanged.
- v2/v3: `access` is a scalar (`r`/`w`/`rw`) and bases live in a
  separate `index_bases` array — converted to the v4 shape here.
"""

from __future__ import annotations

from generator.build import _normalized_memory_map


class TestNormalizedMemoryMapV4:
    def test_v4_passes_entries_through_unchanged(self):
        data = {
            "meta": {"schema_version": 4},
            "memory_map": [
                {"addr": 0, "name": "a", "access": ["r", "w", "b"]},
                {"addr": 1, "name": "b", "access": ["b"]},
            ],
        }
        assert _normalized_memory_map(data) == data["memory_map"]

    def test_v4_ignores_any_stray_index_bases_key(self):
        # Under v4 bases live in `memory_map`; a stray `index_bases`
        # (shouldn't occur) must not be folded in a second time.
        data = {
            "meta": {"schema_version": 4},
            "memory_map": [{"addr": 0, "name": "a", "access": ["b"]}],
            "index_bases": [{"addr": 9, "name": "stray"}],
        }
        result = _normalized_memory_map(data)
        assert [e["name"] for e in result] == ["a"]


class TestNormalizedMemoryMapV3:
    def test_scalar_access_becomes_ordered_list(self):
        data = {
            "meta": {"schema_version": 3},
            "memory_map": [
                {"addr": 0, "name": "r_only", "access": "r"},
                {"addr": 1, "name": "w_only", "access": "w"},
                {"addr": 2, "name": "rw", "access": "rw"},
            ],
        }
        result = _normalized_memory_map(data)
        assert [e["access"] for e in result] == [["r"], ["w"], ["r", "w"]]

    def test_index_bases_folded_in_as_b_rows(self):
        data = {
            "meta": {"schema_version": 3},
            "memory_map": [{"addr": 0x80, "name": "owned", "access": "rw"}],
            "index_bases": [
                {"addr": 0, "name": "base0", "group": "zero_page",
                 "description": "a base"},
            ],
        }
        result = _normalized_memory_map(data)
        assert len(result) == 2
        base = result[-1]
        assert base["name"] == "base0"
        assert base["access"] == ["b"]
        # Other metadata (group, description) is preserved.
        assert base["group"] == "zero_page"
        assert base["description"] == "a base"

    def test_entry_without_access_is_left_alone(self):
        data = {
            "meta": {"schema_version": 3},
            "memory_map": [{"addr": 0, "name": "no_access"}],
        }
        assert "access" not in _normalized_memory_map(data)[0]

    def test_absent_schema_version_treated_as_pre_v4(self):
        # v2 documents omit schema_version entirely (default 2).
        data = {
            "memory_map": [{"addr": 0, "name": "a", "access": "rw"}],
            "index_bases": [{"addr": 1, "name": "b"}],
        }
        result = _normalized_memory_map(data)
        assert [e["access"] for e in result] == [["r", "w"], ["b"]]

    def test_does_not_mutate_input_entries(self):
        entry = {"addr": 0, "name": "a", "access": "rw"}
        data = {"meta": {"schema_version": 3}, "memory_map": [entry]}
        _normalized_memory_map(data)
        assert entry["access"] == "rw"
