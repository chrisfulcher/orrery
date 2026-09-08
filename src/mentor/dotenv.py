"""Read and write the ``.env`` file the way a person would: keep comments, order, and keys
that are not ours; change only the ``MENTOR_*`` lines asked for; add new keys at the end.

The file is the one configuration store (DESIGN.md §3): the app writes it so that Docker,
scripts, and the CLI read the same thing. Writes go to a temporary file replaced atomically,
falling back to an in-place write where the file cannot be replaced (a bind mount), and end
with mode 0600 because the file holds keys. Values never appear in exceptions or logs.
"""

import errno
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path

PREFIX = "MENTOR_"
LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def read(path: Path) -> dict[str, str]:
    """``KEY=VALUE`` pairs, quotes stripped, comments ignored; the last occurrence wins."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        match = LINE.match(raw)
        if match is None or raw.lstrip().startswith("#"):
            continue
        values[match.group(1)] = _unquote(match.group(2).strip())
    return values


def write(path: Path, updates: Mapping[str, str | None]) -> None:
    """Set each ``MENTOR_*`` key to its value (``None`` means ``KEY=``, use the default)."""
    for key in updates:
        if not key.startswith(PREFIX):
            raise ValueError(f"only {PREFIX}* keys belong in .env, not {key!r}")
    if path.is_dir():
        raise ValueError(f"{path} is a directory, not a file")
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    pending = dict(updates)
    for index, raw in enumerate(lines):
        match = LINE.match(raw)
        if match is None or raw.lstrip().startswith("#"):
            continue
        key = match.group(1)
        if key in pending:
            lines[index] = f"{key}={quote(pending.pop(key))}"
    if pending:
        if lines and lines[-1].strip():
            lines.append("")
        lines += [f"{key}={quote(value)}" for key, value in pending.items()]
    _replace(path, "\n".join(lines) + "\n")


def quote(value: str | None) -> str:
    """A value as ``.env`` readers expect: bare when plain, double-quoted otherwise."""
    if value is None or value == "":
        return ""
    if re.fullmatch(r"[A-Za-z0-9_./:@+,%~-]+", value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1]
        if value[0] == '"':
            inner = inner.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return value.split(" #", 1)[0].rstrip()


def _replace(path: Path, text: str) -> None:
    """Write atomically through a temporary file; when the file is a bind mount or its
    directory is not writable (the container's /app), rewrite it in place instead."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".env.", suffix=".tmp")
    except PermissionError:
        path.write_text(text, encoding="utf-8")
        _restrict(path)
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o600)
        try:
            os.replace(tmp, path)
        except OSError as exc:
            if exc.errno not in (errno.EBUSY, errno.EXDEV, errno.EPERM):
                raise
            path.write_text(text, encoding="utf-8")
            Path(tmp).unlink(missing_ok=True)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    _restrict(path)


def _restrict(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except PermissionError:  # another user's file on a mount: the content is written
        pass
