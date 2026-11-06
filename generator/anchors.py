"""Listing-row anchors: how they are named and how addresses resolve to them.

Every row of a disassembly listing carries an HTML id so that operand
links, come-from popups, the subroutine index, inline `address:` /
`label:` links and links from other pages (docs, analyses, the memory
map) can land on it. This module is the single place that decides what
those ids look like and which row an address resolves to.

Today an anchor is named purely by address (`addr-XXXX`). An item has a
*run* address (`addr`, where the code executes) and, if it is relocated,
a *stored* address (`binary_addr`, where the bytes sit in the image).
Both kinds of anchor are emitted. Links written inside the listing
resolve against run addresses only, whereas links from other pages may
also land on a stored address.

Naming anchors by address alone breaks when two rows share an address,
as with two moves to the same run address, or a stored range that
overlaps a run range (acornaeology.github.io#15). Keeping the naming and
resolution here means a move-aware scheme can replace them without
touching the renderers.
"""

import bisect


def anchor_id(addr):
    """The id of the row at run address `addr`."""
    return f"addr-{addr:04X}"


def row_anchor_id(entry):
    """The id of the listing row for `entry`.

    `entry` is a listing item, or a subroutine or banner that heads the
    row for the item at its address. Its run address `addr` names the
    row.
    """
    return anchor_id(entry["addr"])


def stored_anchor_id(entry):
    """The id marking a relocated row's stored (pre-move) address, or
    None if `entry` isn't relocated.

    Currently in the same namespace as run-address anchors (see the
    module docstring).
    """
    binary_addr = entry.get("binary_addr")
    if binary_addr is None:
        return None
    return f"addr-{binary_addr:04X}"


def nearest_at_or_before(sorted_addrs, addr):
    """The largest address in `sorted_addrs` that is <= `addr`, or None.

    For an address inside a multi-byte item this is the item's own
    address.
    """
    idx = bisect.bisect_right(sorted_addrs, addr) - 1
    if idx >= 0:
        return sorted_addrs[idx]
    return None


class AnchorIndex:
    """The anchors one listing page emits, for links from other pages.

    Build one per listing with `from_items`. Docs, analyses and memory-map
    pages hold onto it, keyed by the listing's page id, and resolve their
    `address:` links through it. It covers run and stored addresses
    alike, so a link from another page can land on either. Links within
    the listing resolve against run addresses only (see
    `markdown_listing`).
    """

    def __init__(self, run_addrs=(), stored_addrs=()):
        self._addrs = sorted(set(run_addrs) | set(stored_addrs))
        self._addr_set = set(self._addrs)

    @classmethod
    def from_items(cls, items):
        """Index the run and stored addresses of a listing's items."""
        run_addrs = [item["addr"] for item in items]
        stored_addrs = [
            item["binary_addr"] for item in items if "binary_addr" in item]
        return cls(run_addrs, stored_addrs)

    def resolve(self, addr):
        """The anchor id a link to `addr` should land on.

        Returns the anchor at `addr` if there is one, otherwise the
        nearest preceding anchor, i.e. the row whose item contains
        `addr`. Returns None if no anchor is at or before `addr`.
        """
        if addr in self._addr_set:
            return anchor_id(addr)
        nearest = nearest_at_or_before(self._addrs, addr)
        if nearest is None:
            return None
        return anchor_id(nearest)
