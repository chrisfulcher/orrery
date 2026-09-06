"""Split extracted text into chunks for embedding.

Chunks are measured in characters, not tokens, so no tokenizer is needed: 1,500 characters
is roughly 350 to 450 English tokens, far below the window of any common embedding model.
Chunks never cross a page boundary (the form feed written by extraction), so every chunk
carries a page number.
"""

from dataclasses import dataclass

from mentor.extract.text import PAGE_SEPARATOR

_BREAKS = ("\n\n", "\n", ". ", " ")  # paragraph, line, sentence, word


@dataclass(frozen=True)
class Chunk:
    index: int
    page: int
    text: str


def chunk_text(text: str, *, size: int = 1500, overlap: int = 200) -> list[Chunk]:
    """Split on page separators first, then within a page at the latest natural break in
    the second half of the window. Whitespace-only pieces are skipped; empty pages still
    count toward page numbers."""
    if not 0 <= overlap < size // 2:
        raise ValueError("overlap must be smaller than half the chunk size")
    chunks: list[Chunk] = []
    for page_number, page in enumerate(text.split(PAGE_SEPARATOR), 1):
        start = 0
        while start < len(page):
            end = min(start + size, len(page))
            if end < len(page):
                end = _break_before(page, start + size // 2, end)
            if piece := page[start:end].strip():
                chunks.append(Chunk(len(chunks), page_number, piece))
            if end == len(page):
                break
            start = end - overlap
    return chunks


def _break_before(page: str, floor: int, end: int) -> int:
    """End of the last paragraph, line, sentence, or word break in [floor, end); else end."""
    for separator in _BREAKS:
        at = page.rfind(separator, floor, end)
        if at != -1:
            return at + len(separator)
    return end
