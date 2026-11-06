"""Listing-row anchors: how they are named and how addresses resolve to them.

Every row of a disassembly listing carries an HTML id so that operand
links, come-from popups, the subroutine index, inline `address:` /
`label:` links and links from other pages (docs, analyses, the memory
map) can land on it. This module is the single place that decides what
those ids look like and which row an address resolves to.

Rows and copies
---------------

An item has a *run* address (`addr`, where the code executes) and, if it
is relocated, a *stored* address (`binary_addr`, where its bytes sit in
the image). With moves, several rows can share a run address (two
overlays of the same RAM), and a stored address can equal another row's
run address. dasmos (>= 4.4.0) names the move holding each item
(`move`) and lists the moves in declaration order (`meta.moves`). A row
is identified by `(addr, binary_addr)`, and its copy by `move`.

Anchor ids are permalinks
-------------------------

Each run address and each stored address *claims* the plain id
`addr-XXXX`. Existing ids are permalinks and never change, so:

- A plain id claimed by one row belongs to that row. This holds for
  every id in a listing without overlapping moves.
- When several rows claim the same plain id (ids that were duplicated,
  and so broken, before this scheme), it goes to the highest-ranked
  claim: unmoved code first, then moved code in `meta.moves` order, then
  stored addresses. The other claimants are qualified: a run address as
  `addr-XXXX@MOVE`, a stored address as `stored-XXXX`. A stored address
  belongs to exactly one move, so it needs no move name.
- Every row dasmos marks as moved also carries its qualified ids as
  aliases, so a moved copy keeps a stable permalink even if a later move
  makes its plain id ambiguous.

A link that doesn't say which copy it means resolves to the owner of the
plain id, so it lands where the plain `#addr-XXXX` fragment does. Links
can name a copy with the `?move=NAME`, `?unmoved` or `?stored` flags
(see `parse_link_flags`).

JSON from dasmos before 4.4.0 carries no `move`, so overlapping copies
can't be told apart. Their ids are then left duplicated, exactly as
before, and listed in `unresolved_duplicates`.
"""

import bisect
import re
from collections import defaultdict
from dataclasses import dataclass, field


def anchor_id(addr):
    """The plain id for address `addr`."""
    return f"addr-{addr:04X}"


def move_anchor_id(addr, move):
    """The id of run address `addr` in the copy stored in `move`."""
    return f"addr-{addr:04X}@{move}"


def stored_anchor_id(binary_addr):
    """The qualified id of stored address `binary_addr`."""
    return f"stored-{binary_addr:04X}"


def row_key(entry):
    """The identity of the listing row for `entry`.

    `entry` is a listing item, or a subroutine or banner that heads the
    row of the item at the same addresses.
    """
    return (entry["addr"], entry.get("binary_addr"))


def nearest_at_or_before(sorted_addrs, addr):
    """The largest address in `sorted_addrs` that is <= `addr`, or None.

    For an address inside a multi-byte item this is the item's own
    address.
    """
    idx = bisect.bisect_right(sorted_addrs, addr) - 1
    if idx >= 0:
        return sorted_addrs[idx]
    return None


# --- Link flags ------------------------------------------------------------

@dataclass
class LinkFlags:
    """The `?flag[&flag...]` suffix of an `address:` or `label:` link.

    - `hex`: append ` (&XXXX)` as a second link.
    - `move=NAME`: the copy stored in move NAME.
    - `unmoved`: the copy that runs where it is stored.
    - `stored`: the row whose *stored* address this is.

    At most one of the copy selectors may be given.
    """

    hex: bool = False
    move: str | None = None
    unmoved: bool = False
    stored: bool = False
    unknown: list = field(default_factory=list)

    @property
    def selects_copy(self):
        return self.move is not None or self.unmoved or self.stored

    @property
    def error(self):
        """Why these flags can't be honoured, or None."""
        if self.unknown:
            return "unknown flag " + ", ".join(f"'?{u}'" for u in self.unknown)
        if sum([self.move is not None, self.unmoved, self.stored]) > 1:
            return "give at most one of ?move=, ?unmoved and ?stored"
        return None

    def __str__(self):
        parts = []
        if self.move is not None:
            parts.append(f"move={self.move}")
        if self.unmoved:
            parts.append("unmoved")
        if self.stored:
            parts.append("stored")
        if self.hex:
            parts.append("hex")
        parts.extend(self.unknown)
        return "&".join(parts)


def parse_link_flags(flag_str):
    """Parse a link's flag string (the part after `?`).

    Flags are separated by `&`, which may arrive HTML-escaped (`&amp;`)
    when the link has already been through a Markdown converter. Flag
    words are case-insensitive; a move name is case-sensitive.
    """
    flags = LinkFlags()
    for token in re.split(r"&amp;|&", flag_str or ""):
        token = token.strip()
        if not token:
            continue
        word = token.lower()
        if word == "hex":
            flags.hex = True
        elif word == "stored":
            flags.stored = True
        elif word == "unmoved":
            flags.unmoved = True
        elif word.startswith("move=") and len(token) > len("move="):
            flags.move = token[len("move="):]
        else:
            flags.unknown.append(token)
    return flags


# --- The index -------------------------------------------------------------

_RUN = "run"
_STORED = "stored"


@dataclass(frozen=True)
class Resolution:
    """Where a link lands: an anchor `id`, and whether the address had
    more than one candidate row, so that the default copy was chosen for
    the author."""

    id: str
    ambiguous: bool = False


class AnchorIndex:
    """The anchors one listing page emits, and lookups into them.

    Build one per listing with `from_data`. The listing renderer takes
    each row's ids from it. Docs, analyses and memory-map pages hold onto
    it, keyed by the listing's page id, and resolve their links through
    it.
    """

    def __init__(self, items=(), moves=()):
        self._moves = list(moves)
        move_rank = {name: i for i, name in enumerate(self._moves)}

        self._rows = {}                     # row key -> item
        self._row_by_copy = {}              # (addr, move) -> row key
        run_addrs = defaultdict(set)        # move -> {run addr}
        self._row_by_stored = {}            # binary addr -> row key
        self._label_rows = {}               # label name -> row key
        claims = defaultdict(list)          # plain addr -> [claim]

        for pos, item in enumerate(items):
            key = row_key(item)
            if key in self._rows:
                continue
            self._rows[key] = item
            addr = item["addr"]
            move = item.get("move")
            binary_addr = item.get("binary_addr")
            self._row_by_copy.setdefault((addr, move), key)
            run_addrs[move].add(addr)

            # A claim is (rank, document position, kind, row key).
            if move is None and binary_addr is None:
                run_rank = (0, 0)
            else:
                run_rank = (1, move_rank.get(move, len(move_rank)))
            claims[addr].append((run_rank, pos, _RUN, key))
            if binary_addr is not None:
                self._row_by_stored.setdefault(binary_addr, key)
                claims[binary_addr].append(
                    ((2, run_rank[1]), pos, _STORED, key))

            for name in item.get("labels", []):
                self._label_rows.setdefault(name, key)
            for names in item.get("sub_labels", {}).values():
                for name in names:
                    self._label_rows.setdefault(name, key)

        self._run_by_move = {m: sorted(a) for m, a in run_addrs.items()}
        self._run_set = set().union(*run_addrs.values()) if run_addrs else set()
        self._all_run = sorted(self._run_set)
        self._all_stored = sorted(self._row_by_stored)
        self._all = sorted(self._run_set | set(self._all_stored))

        self._ids = {}                      # (kind, row key) -> id
        self._claims = {}                   # plain addr -> ranked claims
        self.unresolved_duplicates = []
        for addr, addr_claims in claims.items():
            addr_claims.sort(key=lambda c: (c[0], c[1]))
            self._claims[addr] = addr_claims
            for i, (_, _, kind, key) in enumerate(addr_claims):
                self._ids[(kind, key)] = self._claim_id(i, addr, kind, key)

        self._aliases = {}                  # (kind, row key) -> [id]
        for key, item in self._rows.items():
            move = item.get("move")
            if move is None:
                continue
            aliases = [(_RUN, move_anchor_id(item["addr"], move))]
            if item.get("binary_addr") is not None:
                aliases.append(
                    (_STORED, stored_anchor_id(item["binary_addr"])))
            for kind, alias in aliases:
                if alias != self._ids.get((kind, key)):
                    self._aliases.setdefault((kind, key), []).append(alias)

    def _claim_id(self, rank_index, addr, kind, key):
        if rank_index == 0:
            return anchor_id(addr)
        if kind == _STORED:
            return stored_anchor_id(addr)
        move = self._rows[key].get("move")
        if move is not None:
            return move_anchor_id(addr, move)
        # Nothing to qualify by: JSON from dasmos before 4.4.0.
        self.unresolved_duplicates.append(anchor_id(addr))
        return anchor_id(addr)

    @classmethod
    def from_data(cls, data):
        """Index a disassembly document's items and moves."""
        moves = [m["name"] for m in data.get("meta", {}).get("moves", [])]
        return cls(data["items"], moves)

    @classmethod
    def from_addrs(cls, run_addrs):
        """An index of unmoved rows at `run_addrs`, for rendering
        fragments outside a full listing (e.g. in tests)."""
        return cls([{"addr": a} for a in sorted(set(run_addrs))])

    # -- Row ids ----------------------------------------------------------

    def row_ids(self, entry):
        """`(id, aliases)` for the row of `entry` (an item, or the
        subroutine or banner heading it)."""
        key = row_key(entry)
        ident = self._ids.get((_RUN, key)) or anchor_id(entry["addr"])
        return ident, list(self._aliases.get((_RUN, key), []))

    def stored_ids(self, entry):
        """`(id, aliases)` for a relocated row's stored-address anchor,
        or `(None, [])` if `entry` isn't relocated."""
        if entry.get("binary_addr") is None:
            return None, []
        key = row_key(entry)
        ident = (self._ids.get((_STORED, key))
                 or anchor_id(entry["binary_addr"]))
        return ident, list(self._aliases.get((_STORED, key), []))

    # -- Links within the listing ----------------------------------------

    def _default_run_key(self, addr):
        for _, _, kind, key in self._claims.get(addr, []):
            if kind == _RUN:
                return key
        return None

    def _copy_key(self, addr, move):
        return (self._row_by_copy.get((addr, move))
                or self._default_run_key(addr))

    def link_id(self, addr, move=None):
        """The id for a link to the row at run address `addr`.

        Prefers the copy stored in `move` (None: the unmoved copy),
        falling back to the default copy.
        """
        key = self._copy_key(addr, move)
        if key is None:
            return anchor_id(addr)
        return self._ids[(_RUN, key)]

    def copy_item(self, addr, move=None):
        """The item at run address `addr` in the copy stored in `move`
        (None: the unmoved copy), or None if that copy isn't known. A
        relocated row without a `move` (JSON from dasmos before 4.4.0)
        never counts as the unmoved copy."""
        key = self._row_by_copy.get((addr, move))
        if key is None:
            return None
        item = self._rows[key]
        if move is None and item.get("binary_addr") is not None:
            return None
        return item

    def label_link_id(self, name):
        """The id of the row carrying label `name`, or None if no row
        does."""
        key = self._label_rows.get(name)
        return self._ids[(_RUN, key)] if key else None

    def label_flags(self, name):
        """The copy-selecting `LinkFlags` that pin a link to the row
        carrying label `name`, or None if no row carries it."""
        key = self._label_rows.get(name)
        if key is None:
            return None
        move = self._rows[key].get("move")
        if move is not None:
            return LinkFlags(move=move)
        return LinkFlags(unmoved=True)

    # -- Resolving addresses ---------------------------------------------

    def has_move(self, name):
        return name in self._moves or name in self._run_by_move

    def resolve(self, addr, flags=None, *, include_stored=True,
                within_range=False):
        """Resolve a link to `addr` to a `Resolution`, or None if no
        anchor is at or before `addr` in the copy asked for.

        `flags` (a `LinkFlags`) may select a copy. Without one, the link
        lands on the owner of the plain id at `addr`, or at the nearest
        preceding anchor (the row whose item contains `addr`).

        `include_stored` lets a link land on stored addresses too, as
        links from other pages may. Links within the listing resolve
        against run addresses only.

        `within_range` also rejects addresses beyond the last candidate,
        which are more likely mistakes than references into the last row.
        """
        flags = flags or LinkFlags()
        if flags.stored:
            return self._resolve_in(
                addr, self._all_stored, within_range,
                lambda a: self._ids[(_STORED, self._row_by_stored[a])])
        if flags.move is not None or flags.unmoved:
            move = flags.move
            return self._resolve_in(
                addr, self._run_by_move.get(move, []), within_range,
                lambda a: self._ids[(_RUN, self._row_by_copy[(a, move)])])

        candidates = self._all if include_stored else self._all_run
        target = nearest_at_or_before(candidates, addr)
        if target is None or (within_range and addr > candidates[-1]):
            return None
        kinds = (_RUN, _STORED) if include_stored else (_RUN,)
        eligible = [c for c in self._claims[target] if c[2] in kinds]
        _, _, kind, key = eligible[0]
        return Resolution(self._ids[(kind, key)], ambiguous=len(eligible) > 1)

    @staticmethod
    def _resolve_in(addr, sorted_addrs, within_range, id_for):
        target = nearest_at_or_before(sorted_addrs, addr)
        if target is None or (within_range and addr > sorted_addrs[-1]):
            return None
        return Resolution(id_for(target))
