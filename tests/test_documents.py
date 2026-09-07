import pytest

from mentor import documents
from mentor.documents import (
    DEFAULT_WORKFLOW,
    DocumentError,
    ProfileDocument,
    SearchDocument,
    WorkflowDocument,
    parse,
    render_profile,
    render_search,
    render_workflow,
)


def test_empty_template_round_trips() -> None:
    text = render_profile(ProfileDocument())
    assert parse(text, ProfileDocument) == ProfileDocument()


def test_profile_round_trips_with_prose_and_parties() -> None:
    doc = ProfileDocument(
        company={"name": "Example LLC", "uei": "ue9qjd4kk1l6", "cage": "5ute1"},
        offerings={
            "naics": ["541512", "541511"],
            "psc": ["D399"],
            "keywords": ["help desk", "zero trust"],
            "capability_statement": (
                'Line one with "quotes"\nline two with a \\ backslash\n"""odd"""'
            ),
        },
        markets={
            "agency_prefixes": ["075", "075.7526"],
            "office_codes": ["75R602"],
            "places": ["MD"],
        },
        qualifications={
            "size": "small",
            "set_asides": ["SBA", "SDVOSBC"],
            "certifications": ["SDVOSB"],
        },
        competitors=[
            {"uei": "PHZDZ8SJ5CM1", "name": "CDW GOVERNMENT LLC", "notes": "incumbent at HRSA"}
        ],
        partners=[{"name": "Partner Co"}],
        ai={"notes": "Prefers firm-fixed-price work."},
    )
    text = render_profile(doc)
    parsed = parse(text, ProfileDocument)
    assert parsed == doc
    assert parsed.company.uei == "UE9QJD4KK1L6" and parsed.company.cage == "5UTE1"
    assert text.startswith("# mentor company profile")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[company\nname = 1", "not valid TOML"),
        ('[company]\nnmae = "x"\n', "company.nmae: Extra inputs"),
        ('[company]\nuei = "short"\n', "UEI 'short' is not 12 characters"),
        ('[offerings]\nnaics = ["54151"]\n', "NAICS code '54151' is not 6 characters"),
        ('[offerings]\nnaics = ["54151A"]\n', "six digits"),
        ('[qualifications]\nsize = "huge"\n', "qualifications.size"),
    ],
)
def test_problems_become_one_readable_error(text: str, message: str) -> None:
    with pytest.raises(DocumentError, match=message):
        parse(text, ProfileDocument)


def test_strings_are_escaped_for_toml() -> None:
    assert documents._s('a "b" \\ c') == '"a \\"b\\" \\\\ c"'
    assert documents._list(["x", "y"]) == '["x", "y"]'
    assert documents._text("") == '""'


def test_default_workflow_is_the_shipley_style_six() -> None:
    doc = parse(DEFAULT_WORKFLOW, WorkflowDocument)
    assert doc.keys() == ["identify", "qualify", "capture", "proposal", "submitted", "post-award"]
    assert [s.gate for s in doc.stages] == [
        "Pursuit Gate", "Capture Gate", "Bid Gate", "Bid Confirmation Gate", None, None,
    ]  # fmt: skip
    assert doc.gated_keys() == ["identify", "qualify", "capture", "proposal"]
    assert (doc.first_key(), doc.last_key()) == ("identify", "post-award")
    assert doc.next_key("proposal") == "submitted" and doc.next_key("post-award") is None
    assert doc.previous_keys("capture") == ["identify", "qualify"]
    assert doc.tasks_for("submitted")[0].startswith("Answer evaluation")
    assert parse(render_workflow(doc), WorkflowDocument) == doc


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("stages = []\n", "at least one stage"),
        ('[[stages]]\nkey = "a"\nname = "A"\n[[stages]]\nkey = "A"\nname = "B"\n', "duplicate"),
        ('[[stages]]\nkey = "no spaces"\nname = "A"\n', "letters, digits, and hyphens"),
        ('[[stages]]\nkey = "a"\nname = "A"\ngates = "x"\n', "Extra inputs"),
    ],
)
def test_workflow_problems(text: str, message: str) -> None:
    with pytest.raises(DocumentError, match=message):
        parse(text, WorkflowDocument)


def test_search_document_round_trips_and_treats_zero_as_unset() -> None:
    doc = SearchDocument(query="help desk", naics=["541512"], deadline_within_days=30)
    text = render_search("it", doc)
    assert parse(text, SearchDocument) == doc and text.startswith('# saved search "it"')
    empty = parse(render_search("x", SearchDocument()), SearchDocument)
    assert empty == SearchDocument() and empty.deadline_within_days is None
