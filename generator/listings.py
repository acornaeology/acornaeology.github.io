"""The listings a project renders, and how links name them.

Each version renders a primary listing and may declare more under
`disassemblies` in its metadata: variants of one program's driver, say,
or the separate programs on a disc. Every listing has a key:

- `VERSION` for the version's primary listing (`disc_master`)
- `VERSION/SLUG` for a listing declared under `disassemblies`
  (`disc_master/editor`)

The key names its page, `VERSION.html` or `VERSION-SLUG.html`.

`address:` and `label:` links name a listing with `@KEY`. `@/SLUG` is
shorthand for a listing in the same version as the page the link is on,
and `@/` for that version's primary listing. With no `@`, a link goes to
the page's own listing (for docs, its version's primary listing;
analyses have none, so must name one).

`ListingLinks` holds what links need to know about every listing in a
project, keyed by listing key: its anchors, its labels and its
memory-map links. Every resolver of `address:` and `label:` links uses
it, so they all agree on what a link reaches.
"""

from dataclasses import dataclass

from .anchors import AnchorIndex


def listing_key(version_id, slug=None):
    """The key of `version_id`'s primary listing, or of its listing
    `slug` declared under `disassemblies`."""
    return f"{version_id}/{slug}" if slug else version_id


def listing_version(key):
    """The version a listing key belongs to."""
    return key.split("/", 1)[0]


def listing_page_stem(key):
    """The filename stem of a listing's page: `disc_master` or
    `disc_master-editor`."""
    return key.replace("/", "-")


def resolve_listing_key(at, default_key):
    """The listing key an `@` qualifier names (given without the `@`).

    An empty or absent qualifier names `default_key`. `/SLUG` and `/`
    are relative to `default_key`'s version, so they need one; without
    it they resolve to None.
    """
    if not at:
        return default_key
    if at.startswith("/"):
        if default_key is None:
            return None
        return listing_key(listing_version(default_key), at[1:])
    return at


@dataclass
class AddressTarget:
    """Where an address link goes: `href`, and whether that is a
    memory-map entry (which takes the memory-map link attributes)
    rather than a listing row. `warning`, if set, says why the link is
    doubtful though it resolved."""
    href: str
    memory_map: bool = False
    warning: str | None = None


@dataclass
class AddressMiss:
    """Why an address link couldn't be resolved."""
    reason: str


class ListingLinks:
    """Anchors, labels and memory-map links of a project's listings,
    each a dict keyed by listing key:

    - `anchors`: `{key: AnchorIndex}`
    - `mm_links`: `{key: {addr: href}}`, memory-map entries
    - `labels`: `{key: {name: addr}}`
    """

    def __init__(self, anchors=None, mm_links=None, labels=None):
        self.anchors = anchors if anchors is not None else {}
        self.mm_links = mm_links if mm_links is not None else {}
        self.labels = labels if labels is not None else {}

    def __contains__(self, key):
        return key in self.anchors

    def address_target(self, key, addr, flags, *, same_page=False,
                       include_stored=True, within_range=False):
        """Resolve `addr` in listing `key`, in the copy `flags` selects.

        A memory-map entry wins unless the flags pick a copy of moved
        code. Otherwise the link goes to the listing row at or before
        `addr`. `same_page` gives a bare `#fragment` for a link from the
        listing's own page. `include_stored` and `within_range` are as
        for `AnchorIndex.resolve`.
        """
        if key not in self.anchors:
            return AddressMiss("unknown version or listing")
        if flags.error:
            return AddressMiss(flags.error)
        mm_href = self.mm_links.get(key, {}).get(addr)
        if mm_href is not None and not (flags.move or flags.stored):
            return AddressTarget(mm_href, memory_map=True)
        anchors = self.anchors[key]
        if flags.move is not None and not anchors.has_move(flags.move):
            return AddressMiss(f"unknown move '{flags.move}'")
        resolution = anchors.resolve(addr, flags,
                                     include_stored=include_stored,
                                     within_range=within_range)
        if resolution is None:
            return AddressMiss(f"no anchor at or before &{addr:04X}")
        page = "" if same_page else f"{listing_page_stem(key)}.html"
        warning = None
        if resolution.ambiguous:
            warning = (f"several copies are at &{addr:04X}; linking to "
                       f"#{resolution.id}. Name one with ?move=NAME, "
                       f"?unmoved or ?stored")
        return AddressTarget(f"{page}#{resolution.id}", warning=warning)

    def label_addr(self, key, name):
        """The address of label `name` in listing `key`, or None."""
        return self.labels.get(key, {}).get(name)

    def label_flags(self, key, name):
        """The copy-selecting flags for the row carrying label `name` in
        listing `key` (see `AnchorIndex.label_flags`), or None."""
        anchors = self.anchors.get(key)
        return anchors.label_flags(name) if anchors else None

    def listing_anchors(self, key):
        return self.anchors.get(key) or AnchorIndex()
