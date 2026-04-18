"""Render py8dis-originated Markdown into HTML for the disassembly listing.

The py8dis JSON carries the raw Markdown source for subroutine titles,
descriptions, standalone comments, inline comments, and register
descriptions. This module parses that source as CommonMark (plus GFM
tables) via `mistletoe` and emits HTML suited to the per-version
disassembly listing page:

- code fences get `<pre><code class="language-XXX">...</code></pre>`
  so a client-side highlighter can be added later with no renderer
  change;
- bare `&XXXX` addresses inside text nodes become anchor links to
  the same-page `#addr-XXXX` (the "legacy" auto-link continues to
  work so existing comments stay clickable without conversion);
- `[label](address:HEX[?hex])` links are emitted by mistletoe as
  ordinary `<a href="address:...">` tags; the existing
  `apply_address_uri_links` HTML post-processor in build.py rewrites
  those to the correct `#addr-XXXX` anchor.

Two rendering modes are exposed:

- `render_markdown(..., inline=False)` -- block-level rendering for
  subroutine descriptions and standalone (pre-instruction) comments.
  Produces paragraphs, lists, tables, code blocks, the lot.

- `render_markdown(..., inline=True)` -- inline rendering for
  subroutine titles, single-line inline comments, and register-
  cell descriptions. Strips the outer paragraph so the output
  drops cleanly into an <h3>, a <td>, or an inline comment span.
"""

import re
from html import escape as html_escape

import mistletoe
from markupsafe import Markup
from mistletoe.html_renderer import HTMLRenderer


# Bare `&XXXX` in a text node that should auto-link to an anchor on the
# same page. The HTML escaper turns `&` into `&amp;`, so the pattern we
# scan for *after* rendering is `&amp;XXXX`. (We'd rather post-process
# on the rendered HTML than hook a new regex into every render_raw_text
# call -- it's simpler and keeps the default escaping behaviour.)
_BARE_ADDR_RE = re.compile(r'&amp;([0-9A-Fa-f]{4,})')

# Mark off HTML regions where bare-address auto-linking must NOT run:
# - inside an existing <a>...</a> (double-linking would corrupt the DOM)
# - inside <code>...</code> (addresses inside code should be literal --
#   code spans often carry machine code that happens to use &)
_SKIP_REGION_RE = re.compile(
    r'<(?P<tag>a|code)\b[^>]*>.*?</(?P=tag)>',
    re.IGNORECASE | re.DOTALL,
)


_ADDRESS_URI_TARGET_RE = re.compile(
    r'^address:'
    r'(?P<hex>[0-9A-Fa-f]{4,})'
    r'(?:@[^?]+)?'         # @version: ignored in listing context
    r'(?:\?(?P<flag>[^&]*))?'
    r'$',
    re.IGNORECASE,
)


class ListingHTMLRenderer(HTMLRenderer):
    """HTMLRenderer subclass customised for same-page disassembly listings.

    Two overrides:

    - `render_code_fence` tags the emitted `<code>` with a
      `language-XXX` class, matching the highlight.js / Prism
      convention, so future client-side highlighting can slot in
      without renderer changes.

    - `render_link` resolves `address:HEX[@version][?hex]` targets to
      `#addr-HEX` anchors on the current listing page. The `?hex` flag
      appends a second link to the same anchor displaying the hex
      itself (symmetric with how writeup-doc rendering in build.py
      handles the same syntax). `@version` is ignored in the
      listing context -- comments in a listing always refer to their
      own version.
    """

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
        href = f"#addr-{hex_str}"
        inner = self.render_inner(token)

        if flag == "hex":
            return (f'<a href="{href}">{inner}</a> '
                    f'(<a href="{href}"><code>&amp;{hex_str}</code></a>)')
        return f'<a href="{href}">{inner}</a>'


def render_markdown(text, valid_addrs, sorted_addrs, *, inline=False):
    """Render `text` (Markdown) as HTML for the disassembly listing.

    `valid_addrs` / `sorted_addrs` supply the same-page anchor set
    used to auto-link bare `&XXXX` references in text nodes.

    `inline=True` strips the outer `<p>` wrapper, which is what
    callers want for single-line contexts (titles, inline comments,
    register cells).
    """

    if not text:
        return Markup("")

    with ListingHTMLRenderer() as renderer:
        doc = mistletoe.Document(text)
        html = renderer.render(doc)

    html = _autolink_bare_addrs(html, valid_addrs, sorted_addrs)

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


def _autolink_bare_addrs(html, valid_addrs, sorted_addrs):
    """Find bare `&XXXX` (post-escape: `&amp;XXXX`) references in text
    nodes and wrap them in `<a href="#addr-XXXX">` links, matching
    the behaviour of the legacy `_linkify_comment_text` auto-linker.

    Skips any regions inside `<a>...</a>` or `<code>...</code>` so we
    don't double-link or linkify literals.
    """

    # Collect no-go regions first: start/end offsets we must not touch.
    skip_spans = [(m.start(), m.end()) for m in _SKIP_REGION_RE.finditer(html)]

    def in_skip_region(pos):
        for start, end in skip_spans:
            if start <= pos < end:
                return True
        return False

    def rewrite(m):
        if in_skip_region(m.start()):
            return m.group(0)
        hex_digits = m.group(1)
        if len(hex_digits) != 4:
            return m.group(0)
        addr = int(hex_digits, 16)
        target = _resolve_addr(addr, valid_addrs, sorted_addrs)
        if target is None:
            return m.group(0)
        addr_id = f"addr-{target:04X}"
        return f'<a href="#{addr_id}">&amp;{hex_digits}</a>'

    return _BARE_ADDR_RE.sub(rewrite, html)


def _resolve_addr(addr, valid_addrs, sorted_addrs):
    """Return the item address to link to for `addr`.

    Mirrors `disassembly._resolve_addr`: if `addr` is an exact item
    boundary, returns it; otherwise returns the nearest preceding
    item address. Returns None if `addr` is before the first item.
    """
    import bisect
    if addr in valid_addrs:
        return addr
    idx = bisect.bisect_right(sorted_addrs, addr) - 1
    if idx >= 0:
        return sorted_addrs[idx]
    return None
