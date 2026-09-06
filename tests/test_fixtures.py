"""Guards on the captured SAM.gov fixtures: shape assumptions and scrubbing."""

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
