import pytest

from mentor import documents
from mentor.documents import DocumentError, ProfileDocument, parse, render_profile


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
