import pytest

from mentor.embed.chunks import Chunk, chunk_text


def test_empty_and_separator_only_text() -> None:
    assert chunk_text("") == []
    assert chunk_text("\f\f") == []


def test_short_page_is_one_chunk() -> None:
    assert chunk_text("Just a line.") == [Chunk(0, 1, "Just a line.")]


def test_pages_number_from_one_and_empty_pages_count() -> None:
    assert [(c.index, c.page, c.text) for c in chunk_text("a\fb\fc")] == [
        (0, 1, "a"),
        (1, 2, "b"),
        (2, 3, "c"),
    ]
    assert [(c.page, c.text) for c in chunk_text("a\f\fc")] == [(1, "a"), (3, "c")]


def test_long_page_splits_at_sentences_with_overlap() -> None:
    sentences = [
        f"Sentence number {i:02d} says something of moderate length here. " for i in range(60)
    ]
    page = "".join(sentences).strip()

    chunks = chunk_text(page)

    assert len(chunks) > 1
    assert all(len(c.text) <= 1500 for c in chunks)
    assert all(c.text.endswith(".") for c in chunks)
    assert chunks[-1].text.endswith(sentences[-1].strip())
    for previous, current in zip(chunks, chunks[1:], strict=False):
        assert current.text[:60] in previous.text
    assert [c.page for c in chunks] == [1] * len(chunks)


def test_bad_overlap_is_rejected() -> None:
    with pytest.raises(ValueError):
        chunk_text("x", size=100, overlap=50)
