"""A form built from field specs: labels above inputs, one widget per field, values back as
a nested dict. Kinds: text, secret (masked), csv (comma-separated list), int, float, prose
(multi-line), select, lines (one item per line), parties (one ``UEI | name | notes`` per
line). A locked field is shown disabled with the reason. Errors are one line at the top."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Input, Label, Select, Static, TextArea

Kind = Literal["text", "secret", "csv", "int", "float", "prose", "select", "lines", "parties"]


@dataclass(frozen=True)
class FieldSpec:
    key: str
    """Dotted for nested documents (``company.name``), flat for settings."""
    label: str
    kind: Kind = "text"
    empty: object = ""
    """What an empty widget means: '' or None or []."""
    options: tuple[tuple[str, str], ...] = ()
    """(label, value) pairs for a select; a blank choice is offered when ``empty`` is None."""
    help: str = ""
    locked: str | None = None
    """A reason: the field is shown disabled."""
    section: str | None = None
    """A heading rendered before this field."""


class FormError(ValueError):
    def __init__(self, key: str, message: str) -> None:
        super().__init__(f"{key}: {message}")
        self.key = key


def widget_id(key: str) -> str:
    return "field-" + key.replace(".", "-").replace("_", "-")


def flatten(data: Mapping, prefix: str = "") -> dict[str, object]:
    flat: dict[str, object] = {}
    for key, value in data.items():
        name = f"{prefix}{key}"
        if isinstance(value, Mapping):
            flat.update(flatten(value, name + "."))
        else:
            flat[name] = value
    return flat


def unflatten(flat: Mapping[str, object]) -> dict:
    data: dict = {}
    for key, value in flat.items():
        parts = key.split(".")
        node = data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return data


def parties_lines(parties: Sequence[Mapping]) -> str:
    return "\n".join(
        " | ".join(str(p.get(k) or "") for k in ("uei", "name", "notes")).rstrip(" |")
        for p in parties
    )


def parse_parties(text: str) -> list[dict]:
    parties = []
    for line in text.splitlines():
        if not line.strip():
            continue
        cells = [c.strip() for c in line.split("|")] + ["", "", ""]
        uei, name, notes = cells[:3]
        parties.append({"uei": uei or None, "name": name, "notes": notes})
    return parties


class Form(VerticalScroll):
    """The fields, in order, with an error line on top."""

    def __init__(
        self, fields: Sequence[FieldSpec], values: Mapping[str, object], *, id: str | None = None
    ) -> None:
        super().__init__(id=id)
        self.fields = list(fields)
        self.initial = dict(values)

    def compose(self) -> ComposeResult:
        yield Static("", id=f"{self.id}-errors", classes="errors")
        for spec in self.fields:
            if spec.section:
                yield Label(spec.section, classes="section")
            yield Label(spec.label + (f"  ({spec.help})" if spec.help else ""), classes="field")
            yield self._widget(spec, self.initial.get(spec.key))
            if spec.locked:
                yield Static(spec.locked, classes="locked")

    def _widget(self, spec: FieldSpec, value: object):
        wid = widget_id(spec.key)
        if spec.kind == "select":
            select = Select(
                [(label, val) for label, val in spec.options],
                allow_blank=spec.empty is None,
                value=Select.NULL if value in (None, "") else value,
                id=wid,
            )
            select.disabled = bool(spec.locked)
            return select
        if spec.kind in ("prose", "lines", "parties"):
            if spec.kind == "lines":
                text = "\n".join(value or [])
            elif spec.kind == "parties":
                text = parties_lines(value or [])
            else:
                text = str(value or "")
            area = TextArea(text, id=wid)
            area.disabled = bool(spec.locked)
            return area
        if spec.kind == "csv":
            text = ", ".join(str(v) for v in (value or []))
        elif value is None:
            text = ""
        else:
            text = str(value)
        widget = Input(value=text, password=spec.kind == "secret", id=wid)
        widget.disabled = bool(spec.locked)
        return widget

    def values(self) -> dict[str, object]:
        """Every unlocked field, converted; raises ``FormError`` on the first bad value."""
        out: dict[str, object] = {}
        for spec in self.fields:
            if spec.locked:
                continue
            widget = self.query_one(f"#{widget_id(spec.key)}")
            if spec.kind == "select":
                out[spec.key] = None if widget.value is Select.NULL else widget.value
                continue
            raw = widget.text if isinstance(widget, TextArea) else widget.value
            text = raw.strip() if spec.kind != "prose" else raw.strip("\n")
            if spec.kind == "lines":
                out[spec.key] = [line.strip() for line in raw.splitlines() if line.strip()]
            elif spec.kind == "parties":
                out[spec.key] = parse_parties(raw)
            elif spec.kind == "csv":
                out[spec.key] = [v.strip() for v in text.split(",") if v.strip()]
            elif not text:
                out[spec.key] = spec.empty
            elif spec.kind == "int":
                try:
                    out[spec.key] = int(text)
                except ValueError:
                    raise FormError(spec.key, f"{spec.label} must be a whole number") from None
            elif spec.kind == "float":
                try:
                    out[spec.key] = float(text)
                except ValueError:
                    raise FormError(spec.key, f"{spec.label} must be a number") from None
            else:
                out[spec.key] = text
        return out

    def show_error(self, message: str | None) -> None:
        errors = self.query_one(f"#{self.id}-errors", Static)
        errors.update(message or "")
        errors.set_class(bool(message), "-visible")

    def focus_field(self, key: str) -> None:
        try:
            self.query_one(f"#{widget_id(key)}").focus()
        except Exception:  # unknown key: leave focus alone
            return
