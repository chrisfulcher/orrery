"""Guards on the captured SAM.gov and USAspending fixtures: shape assumptions and scrubbing."""

import csv
import json
import re
from pathlib import Path

FIXTURES = Path(__file__).with_name("fixtures")


def test_search_fixture_shape_and_scrubbing() -> None:
    data = json.loads((FIXTURES / "sam_search_v2.json").read_text())
    notices = data["opportunitiesData"]
    assert data["totalRecords"] == len(notices) == 5
    for notice in notices:
        assert notice["description"].startswith("https://api.sam.gov/")
        assert re.fullmatch(r"[0-9a-f]{32}", notice["noticeId"])
        assert notice["fullParentPathCode"]
        for contact in notice.get("pointOfContact") or []:
            assert contact["email"].endswith("@example.gov")
            assert contact["fullName"].startswith("Point of Contact")
            assert contact["phone"] is None and contact["fax"] is None


def test_description_fixture_shape() -> None:
    data = json.loads((FIXTURES / "sam_noticedesc_v1.json").read_text())
    assert set(data) == {"description"}
    assert data["description"].startswith("<p>")


def test_usaspending_download_fixtures_shape() -> None:
    ticket = json.loads((FIXTURES / "usaspending_download_awards.json").read_text())
    assert set(ticket) == {"download_request", "file_name", "file_url", "status_url"}
    assert ticket["file_url"].startswith("https://files.usaspending.gov/")
    assert ticket["file_name"] in ticket["status_url"]
    status = json.loads((FIXTURES / "usaspending_download_status.json").read_text())
    assert status["status"] == "finished"
    assert status["file_name"] == ticket["file_name"]
    assert isinstance(status["total_rows"], int)


def test_usaspending_awards_sample_shape_and_scrubbing() -> None:
    with (FIXTURES / "usaspending_awards_sample.csv").open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 8
    assert len(rows[0]) == 286
    for row in rows:
        assert re.fullmatch(r"[A-Z0-9]{12}", row["recipient_uei"])
        assert row["awarding_office_code"] and row["contract_award_unique_key"]
        assert row["naics_code"] == "541512"
        assert row["recipient_phone_number"] == "" and row["recipient_fax_number"] == ""
        for i in range(1, 6):
            assert row[f"highly_compensated_officer_{i}_name"] == ""


def test_entity_v3_fixture_shape_and_scrubbing() -> None:
    data = json.loads((FIXTURES / "sam_entity_v3.json").read_text())
    assert data["totalRecords"] == len(data["entityData"]) == 1
    entity = data["entityData"][0]
    assert re.fullmatch(r"[A-Z0-9]{12}", entity["entityRegistration"]["ueiSAM"])
    assert entity["assertions"]["goodsAndServices"]["primaryNaics"]
    for contact in entity["pointsOfContact"].values():
        if contact.get("firstName") is not None:
            assert contact["firstName"].startswith("Point of Contact")
            assert contact["lastName"] == "Placeholder"
        assert "email" not in contact and "usPhone" not in contact


def test_entity_extract_sample_shape_and_scrubbing() -> None:
    lines = (FIXTURES / "sam_entity_extract_sample.txt").read_text(encoding="utf-8").split("\n")
    assert lines[0].startswith("BOF PUBLIC V2 ") and lines[4].startswith("EOF PUBLIC V2 ")
    assert lines[5] == ""
    codes = set()
    for record in lines[1:4]:
        fields = record.split("|")
        assert len(fields) == 142 and fields[141] == "!end"
        assert re.fullmatch(r"[A-Z0-9]{12}", fields[0])
        assert all(field == "" for field in fields[46:112]), "point-of-contact fields must be blank"
        codes.add(fields[5])
    assert codes == {"A", "E"}
