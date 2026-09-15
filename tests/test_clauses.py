from orrery import clauses


def numbers(text: str) -> list[str]:
    return sorted(clauses.find(text))


def test_a_citation_is_read_with_or_without_its_regulation_name() -> None:
    text = (
        "The contractor shall comply with FAR 52.204-21, with 52.219-14, and with"
        " DFARS 252.204-7012 as incorporated by reference."
    )

    assert numbers(text) == ["252.204-7012", "52.204-21", "52.219-14"]


def test_an_alternate_stays_with_the_number_and_a_deviation_does_not() -> None:
    """The alternate is a different clause and keeps its suffix; a class deviation changes
    the text under the same citation, so it is read and dropped."""
    found = clauses.find(
        "52.222-26 Alternate I applies. So does 52.222-26 Alt. I. See also"
        " 52.219-14 (DEVIATION 2021-O0008) and 252.204-7012 (CLASS DEVIATION 2020-O0021)."
    )

    assert found == {"52.222-26 Alt I": 2, "52.219-14": 1, "252.204-7012": 1}


def test_an_agency_supplement_parses_and_is_labelled_by_its_part() -> None:
    assert clauses.regulation("52") == "FAR"
    assert clauses.regulation("252") == "DFARS"
    assert clauses.regulation("1852") == "supplement"
    assert clauses.regulation("3052") == "supplement"
    assert clauses.regulation("5652") == "supplement"

    (ref,) = clauses.references([("sow.pdf", "NASA clause 1852.245-70 applies.")])

    assert (ref.number, ref.regulation) == ("1852.245-70", "supplement")


def test_the_rest_of_the_regulation_is_not_a_clause() -> None:
    """Clauses live in part 52 of a chapter and nowhere else, so a citation to the
    regulation's own text has the same shape and is not a reference to a clause. Before this
    was enforced, 15.404-1 and 28.307-2 were among the ten most matched numbers in the live
    store."""
    text = (
        "Price will be evaluated using the techniques in FAR 15.404-1(b), insurance under"
        " FAR 28.307-2(b), and the contract type limits of 16.301-3, and the clause at"
        " 52.212-4 applies."
    )

    assert numbers(text) == ["52.212-4"]


def test_what_is_not_a_clause_number() -> None:
    """A one-digit part is a FAR paragraph, not a clause; dates and version strings share
    enough of the shape to be worth refusing on purpose."""
    assert numbers("see 5.204-21 and part 5.204-21 of the FAR") == []
    assert numbers("posted 2026-09-14, amended 09/14/2026, cut 2026.09.14") == []
    assert numbers("reader v1.252-3 and build 2.252-1 of the tool") == []
    assert numbers("account 9952.204-2134567 and 152.2043-21") == []


def test_references_counts_every_mention_and_names_each_document_once() -> None:
    refs = clauses.references(
        [
            ("notice", "Includes 52.219-14 and 52.204-21."),
            ("sow.pdf", "52.219-14 applies. 52.219-14 again. Also DFARS 252.204-7012."),
            ("pricing.xlsx", ""),
        ]
    )

    assert [(ref.number, ref.mentions, ref.attachments) for ref in refs] == [
        ("52.204-21", 1, ("notice",)),
        ("52.219-14", 3, ("notice", "sow.pdf")),
        ("252.204-7012", 1, ("sow.pdf",)),
    ]
    assert [ref.regulation for ref in refs] == ["FAR", "FAR", "DFARS"]
    assert all(ref.title is None for ref in refs)


def test_the_order_is_far_then_dfars_then_supplements_each_in_numeric_order() -> None:
    refs = clauses.references(
        [
            ("a.pdf", "1852.245-70 252.204-7012 52.219-14 52.204-21 52.204-7 252.203-7000"),
            ("b.pdf", "3052.204-70"),
        ]
    )

    assert [ref.number for ref in refs] == [
        "52.204-7",
        "52.204-21",
        "52.219-14",
        "252.203-7000",
        "252.204-7012",
        "1852.245-70",
        "3052.204-70",
    ]


def test_nothing_to_read_is_no_references() -> None:
    assert clauses.references([]) == ()
    assert clauses.references([("sow.pdf", "no clauses here at all")]) == ()
