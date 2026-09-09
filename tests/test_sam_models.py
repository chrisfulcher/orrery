import json
from pathlib import Path

from orrery.sam.models import Opportunity, SearchPage

FIXTURE = Path(__file__).with_name("fixtures") / "sam_search_v2.json"


def test_search_page_keeps_raw_records() -> None:
    raw = json.loads(FIXTURE.read_text())
    page = SearchPage.model_validate(raw)
    assert page.total_records == 5
    assert page.opportunities_data == raw["opportunitiesData"]


def test_opportunity_maps_fixture_fields() -> None:
    raw = json.loads(FIXTURE.read_text())["opportunitiesData"]
    opportunities = [Opportunity.model_validate(record) for record in raw]

    first = opportunities[0]
    assert first.notice_id == raw[0]["noticeId"]
    assert first.notice_type == "Justification"
    assert first.psc_code == "DJ01"
    assert first.naics_code == "541512"
    assert first.set_aside_code is None
    assert first.active is True
    assert first.posted_at == "2026-09-05"
    assert first.description_url.startswith("https://api.sam.gov/")
    assert len(first.resource_links) == 1
    assert first.place_of_performance["state"]["code"] == "MD"

    without_links = next(o for o in opportunities if not o.resource_links)
    assert without_links.resource_links == []


def test_active_accepts_no() -> None:
    opportunity = Opportunity.model_validate({"noticeId": "x", "title": "t", "active": "No"})
    assert opportunity.active is False
