"""Heading ids for rendered Markdown documents, GitHub-style.

Docs and analyses are written to read correctly on GitHub, where
`[text](#some-heading)` jumps to a heading because GitHub gives every
heading an id derived from its text. The site's Markdown converter
doesn't, so this adds the same ids to the rendered HTML. Links into a
heading, within a page or from another document (`other.md#heading`),
then work identically on the site.

The slug follows GitHub's (github-slugger) rules: take the heading's
visible text, lower-case it, drop everything that isn't a letter, mark,
number, `_`, `-` or space, then turn each space into `-`. A repeated
slug gets `-1`, `-2`, … in document order.
"""

import re
import unicodedata
from html import unescape

_HEADING_RE = re.compile(
    r"<h(?P<level>[1-6])(?P<attrs>[^>]*)>(?P<inner>.*?)</h(?P=level)>",
    re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def heading_slug(text):
    """The GitHub slug of a heading's visible `text`."""
    kept = []
    for ch in text.lower():
        if ch in "_- " or unicodedata.category(ch)[0] in "LMN":
            kept.append(ch)
    return "".join(kept).replace(" ", "-")


def add_heading_ids(html):
    """Give every `<h1>`–`<h6>` in `html` that has no id a GitHub-style
    slug id, numbering repeats as GitHub does."""
    seen = set()

    def unique(slug):
        candidate, n = slug, 0
        while candidate in seen:
            n += 1
            candidate = f"{slug}-{n}"
        seen.add(candidate)
        return candidate

    def repl(match):
        attrs = match.group("attrs")
        if re.search(r"\bid\s*=", attrs):
            return match.group(0)
        text = unescape(_TAG_RE.sub("", match.group("inner")))
        slug = heading_slug(text.strip())
        if not slug:
            return match.group(0)
        slug = unique(slug)
        level = match.group("level")
        return (f'<h{level} id="{slug}"{attrs}>'
                f'{match.group("inner")}</h{level}>')

    return _HEADING_RE.sub(repl, html)
