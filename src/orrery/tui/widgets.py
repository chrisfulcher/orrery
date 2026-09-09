"""Widgets shared by the screens."""

from textual.widgets import DataTable

Row = tuple[tuple[str, ...], str | None]
"""Cells and the row key."""


class WrapTable(DataTable):
    """A table whose text wraps instead of truncating: every column has a declared width
    except one, which takes the width that is left, and rows grow to fit their text. The
    table keeps its rows and lays them out again whenever it is resized."""

    MIN_FLEX = 12
    COMFORTABLE_FLEX = 32
    MIN_SHRINK = 12
    """Fixed columns wider than this give width back, down to this, before the flexible
    column drops below COMFORTABLE_FLEX."""

    def __init__(self, columns: list[tuple[str, int | None]], **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.columns_spec = columns
        self.rows_data: list[Row] = []

    def set_rows(self, rows: list[Row]) -> None:
        """Show these rows. Unchanged rows leave the table, its cursor, and its scroll alone,
        so a periodic refresh never disturbs what the user is looking at."""
        if rows == self.rows_data and self.columns:
            return
        self.rows_data = rows
        self._layout_rows()

    def on_resize(self) -> None:
        self._layout_rows()

    def _layout_rows(self) -> None:
        selected = self._selected_key()
        self._rebuild()
        if selected is not None:
            self._select(selected)

    def _selected_key(self) -> str | None:
        if not self.row_count:
            return None
        try:
            return self.coordinate_to_cell_key(self.cursor_coordinate).row_key.value
        except Exception:  # no cell under the cursor
            return None

    def _select(self, key: str) -> None:
        try:
            index = self.get_row_index(key)
        except Exception:  # the row is gone; the cursor stays at the top
            return
        self.move_cursor(row=index, animate=False, scroll=True)

    def _rebuild(self) -> None:
        pad = 2 * self.cell_padding
        widths = [width for _, width in self.columns_spec]
        available = self.content_size.width - pad * len(widths)

        def flex() -> int:
            return available - sum(width for width in widths if width is not None)

        while flex() < self.COMFORTABLE_FLEX:
            widest = max(
                (i for i, w in enumerate(widths) if w is not None and w > self.MIN_SHRINK),
                key=lambda i: widths[i],
                default=None,
            )
            if widest is None:
                break
            widths[widest] -= 1
        self.clear(columns=True)
        for (label, _), width in zip(self.columns_spec, widths, strict=True):
            self.add_column(label, width=max(flex(), self.MIN_FLEX) if width is None else width)
        for cells, key in self.rows_data:
            self.add_row(*cells, height=None, key=key)
