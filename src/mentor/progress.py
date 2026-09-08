"""Progress and cancellation for long-running work, without a dependency on the UI.

A function that runs for minutes accepts ``report`` (a line of progress) and ``cancelled``
(a question it asks between units of work). Both default to no-ops, so callers that do not
care pass nothing. Cancellation is cooperative: the current page, batch, or file finishes,
then ``JobCancelled`` is raised through the function's normal failure path, which closes the
run as failed with the text ``cancelled`` and leaves committed work standing.
"""

from collections.abc import Callable

Report = Callable[[str], None]
Cancelled = Callable[[], bool]


def quiet(message: str) -> None:
    """Discard a progress line."""


def never() -> bool:
    return False


class JobCancelled(Exception):
    """The user cancelled between units of work."""

    def __init__(self) -> None:
        super().__init__("cancelled")


def check(cancelled: Cancelled) -> None:
    if cancelled():
        raise JobCancelled
