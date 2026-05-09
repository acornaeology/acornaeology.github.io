"""Shared test fixtures for the acornaeology static-site generator.

Tests here exercise the renderer functions directly (no filesystem,
no Jinja). Fixtures provide the lookup tables the renderer expects so
each test can author a tiny item / sub dict and assert on the
structured intermediate (line dicts) rather than the rendered HTML.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def empty_lookups():
    """Empty address-lookup tables for tests that don't need link
    resolution. Fields match the kwargs `_process_item` expects.
    """
    return {
        "valid_addrs": set(),
        "sorted_addrs": [],
        "label_tooltips": {},
        "mm_links": {},
    }
