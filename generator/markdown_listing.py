"""Render py8dis-originated Markdown into HTML for the disassembly listing.

The py8dis JSON carries the raw Markdown source for subroutine titles,
descriptions, standalone comments, inline comments, and register
descriptions. This module parses that source as CommonMark (plus GFM
tables) via `mistletoe` and emits HTML suited to the per-version
disassembly listing page.

Address linking is driven entirely by explicit `[label](address:HEX)`
Markdown syntax -- bare `&XXXX` in prose renders as plain text, to
keep the authored intent explicit (and so we don't silently manufacture
a link when the author didn't ask for one).

`address:HEX` targets are resolved in this priority order:

1. Memory-map entry. Links to the per-version memory-map page, with
   `target="memory-map"` so a side-by-side memory-map window is
   reused across clicks.

2. ROM-range anchor. Links to `#addr-HEX` on the same listing page.
   If the exact address has no anchor (the reference points into the
   middle of a multi-byte item), resolves to the nearest preceding
   anchor.

3. Unknown. Left as the input `<a>` with the original `address:HEX`
   href and a build warning; the site renderer's upstream
   `apply_address_uri_links` catches nothing here but the broken
   state is still visible in the output.

`render_link` accepts a `?hex` flag which appends a second hyperlink
of the form `(&XXXX)` to the rendered label, symmetric with the same
flag in writeup-doc rendering.
"""

import re
from html import escape as html_escape

import mistletoe
from markupsafe import Markup
from mistletoe.html_renderer import HTMLRenderer


_ADDRESS_URI_TARGET_RE = re.compile(
    r'^address:'
    r'(?P<hex>[0-9A-Fa-f]{4,})'
    r'(?:@[^?]+)?'         # @version: ignored in listing context
    r'(?:\?(?P<flag>[^&]*))?'
    r'$',
    re.IGNORECASE,
)


class ListingHTMLRenderer(HTMLRenderer):
    """HTMLRenderer subclass customised for the disassembly listing.

    Two overrides:

    - `render_code_fence` tags the emitted `<code>` with a
      `language-XXX` class, matching the highlight.js / Prism
      convention.

    - `render_link` resolves `address:HEX[@version][?hex]` targets
      intelligently -- see the module docstring for the priority
      order. `@version` is ignored in the listing context because
      comments in a listing always refer to their own version.

    The renderer reads `self.mm_links`, `self.valid_addrs`, and
    `self.sorted_addrs` for address resolution. Callers must set
    those attributes on the instance before calling `render` (see
    `render_markdown`).
    """

    def __init__(self):
        super().__init__()
        # Populated by `render_markdown` before each render pass.
        self.mm_links = {}
        self.valid_addrs = set()
        self.sorted_addrs = []

    def render_code_fence(self, token):
        language = getattr(token, "language", "") or ""
        inner = html_escape(token.children[0].content) if token.children else ""
        cls = f' class="language-{html_escape(language)}"' if language else ""
        return f'<pre><code{cls}>{inner}</code></pre>'

    def render_link(self, token):
        target = getattr(token, "target", "") or ""
        m = _ADDRESS_URI_TARGET_RE.match(target)
        if not m:
            return super().render_link(token)

        hex_str = m.group("hex").upper()
        flag = (m.group("flag") or "").lower()
        addr = int(hex_str, 16)
        inner = self.render_inner(token)

        href, extra_attrs = self._resolve_address_href(addr, hex_str)

        if flag == "hex":
            return (f'<a{extra_attrs} href="{href}">{inner}</a> '
                    f'(<a{extra_attrs} href="{href}"><code>&amp;{hex_str}</code></a>)')
        return f'<a{extra_attrs} href="{href}">{inner}</a>'

    def _resolve_address_href(self, addr, hex_str):
        """Resolve `address:HEX` to (href, extra_attrs).

        Priority: memory-map entry -> ROM-range anchor (exact or
        nearest preceding) -> unresolved (leave as `address:HEX`).
        Returns a pair so the caller can attach `class=""` /
        `target=""` for memory-map links without rebuilding them.
        """
        if addr in self.mm_links:
            return self.mm_links[addr], ' class="mm-link" target="memory-map"'

        if addr in self.valid_addrs:
            return f"#addr-{addr:04X}", ""

        if self.sorted_addrs:
            import bisect
            idx = bisect.bisect_right(self.sorted_addrs, addr) - 1
            if idx >= 0:
                nearest = self.sorted_addrs[idx]
                # Only use nearest-preceding if the address is inside
                # the anchored range (between first and last). Outside
                # that range it's neither a memory-map entry nor a ROM
                # reference -- almost certainly an author typo.
                if nearest <= addr <= self.sorted_addrs[-1]:
                    return f"#addr-{nearest:04X}", ""

        print(f"  Warning: address:{hex_str} -- no memory-map entry "
              f"and not in ROM range")
        return f"address:{hex_str}", ""


def render_markdown(text, valid_addrs, sorted_addrs, *, inline=False,
                    mm_links=None):
    """Render `text` (Markdown) as HTML for the disassembly listing.

    `valid_addrs` / `sorted_addrs` supply the ROM-range anchor set
    used to resolve `address:HEX` targets that point into the ROM.

    `mm_links` is `{addr: href}` for non-ROM labels that have a
    memory-map entry on the per-version memory-map page; supply it
    when rendering listing content so those targets pick up the
    cross-window link. Pass `None` (or omit) in other contexts --
    resolution then falls back to ROM-range anchors only.

    `inline=True` strips the outer `<p>` wrapper, which is what
    callers want for single-line contexts (titles, inline comments,
    register cells).
    """

    if not text:
        return Markup("")

    with ListingHTMLRenderer() as renderer:
        renderer.mm_links = mm_links or {}
        renderer.valid_addrs = valid_addrs
        renderer.sorted_addrs = sorted_addrs
        doc = mistletoe.Document(text)
        html = renderer.render(doc)

    if inline:
        html = _strip_outer_paragraph(html)

    return Markup(html)


def _strip_outer_paragraph(html):
    """If `html` is a single top-level <p>...</p>, unwrap it.

    mistletoe wraps inline text in a paragraph; for titles and
    register cells the <p> is visual noise inside an <h3> or <td>.
    """
    stripped = html.strip()
    if stripped.startswith("<p>") and stripped.endswith("</p>"):
        inner = stripped[3:-4]
        # Only unwrap if there's exactly one paragraph -- multiple
        # paragraphs stay intact so caller's markup is still valid.
        if "</p>" not in inner:
            return inner
    return html
