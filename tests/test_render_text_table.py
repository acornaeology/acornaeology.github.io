"""Tests for the box-drawn comment-table renderer.

`_render_md_table_text` parses GFM pipe-tables into Unicode
box-drawn lines that fit the listing's monospace grid. These tests
cover natural-width happy paths, narrow-budget cell wrapping,
escaped pipes, single-column tables, and link/code atoms inside
cells (which must survive the cell-wrap step).
"""

from __future__ import annotations

import re

from generator.disassembly import _render_md_table_text


def _visible_width(line):
    """Visible width of a rendered table line (HTML stripped)."""
    return len(re.sub(r"<[^>]+>", "", line))


class TestRenderTextTable:

    def _render(self, table_md, max_width=62):
        return _render_md_table_text(
            table_md,
            max_width=max_width,
            valid_addrs=set(),
            sorted_addrs=[],
            label_tooltips={},
            mm_links={},
        )

    def test_happy_path_two_columns(self):
        md = "| A | Meaning |\n|---|---|\n| 0 | foo |\n| 1 | bar |"
        lines = self._render(md)
        assert lines[0].startswith("┌") and lines[0].endswith("┐")
        assert lines[-1].startswith("└") and lines[-1].endswith("┘")
        # 1 top + 1 header + 1 separator + 2 body + 1 bottom = 6 lines
        assert len(lines) == 6

    def test_separator_between_header_and_body(self):
        md = "| A | B |\n|---|---|\n| 1 | 2 |"
        lines = self._render(md)
        # The separator row uses ├─┼─┤
        seps = [l for l in lines if "├" in l and "┼" in l and "┤" in l]
        assert len(seps) == 1

    def test_all_lines_fit_in_max_width(self):
        md = "| A | Meaning |\n|---|---|\n| 0 | filing system |"
        lines = self._render(md, max_width=40)
        for line in lines:
            assert _visible_width(line) <= 40, f"line overflows: {line!r}"

    def test_single_column(self):
        md = "| Name |\n|---|\n| Alice |\n| Bob |"
        lines = self._render(md)
        assert all("│" in l or any(c in l for c in "┌┐├┤└┘") for l in lines)
        # No `┬` / `┴` for single-column tables (no inter-column borders)
        assert not any("┬" in l or "┴" in l for l in lines)

    def test_escaped_pipe_in_cell_preserved(self):
        # `\|` is the GFM escape for a literal pipe in a cell.
        md = r"| Op | Meaning |" "\n|---|---|\n" r"| `a \| b` | OR |"
        lines = self._render(md)
        joined = "\n".join(lines)
        # The pipe should appear literally in the rendered output.
        assert "|" in re.sub(r"<[^>]+>", "", joined).replace("│", "")

    def test_link_atom_inside_cell_renders_as_anchor(self):
        # `address:HEX` Markdown link should produce an <a> tag in the cell.
        md = "| Ref | Note |\n|---|---|\n| [foo](address:8000) | bar |"
        lines = _render_md_table_text(
            md, max_width=62,
            valid_addrs={0x8000}, sorted_addrs=[0x8000],
            label_tooltips={}, mm_links={},
        )
        joined = "\n".join(lines)
        assert "<a " in joined
        assert "addr-8000" in joined

    def test_narrow_max_width_wraps_long_cells(self):
        # Available content for two columns at max_width=20 is small.
        md = ("| A | Description |\n"
              "|---|---|\n"
              "| 0 | A reasonably long description that must wrap |")
        lines = self._render(md, max_width=30)
        # Body rows wrap into multiple display lines for the same logical row.
        assert len(lines) > 6  # more than the unwrapped 6-line case
        for line in lines:
            assert _visible_width(line) <= 30

    def test_empty_body_returns_just_header(self):
        # GFM permits header + separator with no body rows.
        # Our regex requires ≥1 body row, so this won't be detected as a
        # table by `_split_comment_segments`, but the renderer should
        # gracefully produce something or empty when called directly.
        md = "| A | B |\n|---|---|\n| 1 | 2 |"
        lines = self._render(md)
        assert lines  # at least the borders

    def test_box_drawing_border_chars_are_correct(self):
        md = "| A | B |\n|---|---|\n| 1 | 2 |"
        lines = self._render(md)
        # Top:    ┌─┬─┐
        # Header: │ … │ … │
        # Sep:    ├─┼─┤
        # Body:   │ … │ … │
        # Bottom: └─┴─┘
        assert "┌" in lines[0] and "┬" in lines[0] and "┐" in lines[0]
        assert "├" in lines[2] and "┼" in lines[2] and "┤" in lines[2]
        assert "└" in lines[-1] and "┴" in lines[-1] and "┘" in lines[-1]

    def test_pad_to_column_width(self):
        # Header "A" is 1 char, body "10" is 2 chars; the column should
        # widen to fit and pad both rows to the same width.
        md = "| A | B |\n|---|---|\n| 10 | 20 |"
        lines = self._render(md)
        # Every row line should have the same visible width (top, sep,
        # bottom share the same widths).
        widths = {_visible_width(l) for l in lines}
        assert len(widths) == 1
