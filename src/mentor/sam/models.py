"""Typed views of SAM.gov Get Opportunities v2 payloads.

Only the fields that map onto ``notices`` columns are typed; the raw record is kept as a
dict by the caller and stored verbatim as ``raw_json``. Dates stay as the strings the API
sends; normalization is the ingestion parse's job.
"""

from typing import Any

from pydantic import BaseModel, Field, field_validator


class Opportunity(BaseModel):
    notice_id: str = Field(validation_alias="noticeId")
    title: str
    solicitation_number: str | None = Field(None, validation_alias="solicitationNumber")
    notice_type: str | None = Field(None, validation_alias="type")
    full_parent_path_name: str | None = Field(None, validation_alias="fullParentPathName")
    full_parent_path_code: str | None = Field(None, validation_alias="fullParentPathCode")
    naics_code: str | None = Field(None, validation_alias="naicsCode")
    psc_code: str | None = Field(None, validation_alias="classificationCode")
    set_aside_code: str | None = Field(None, validation_alias="typeOfSetAside")
    posted_at: str | None = Field(None, validation_alias="postedDate")
    response_deadline: str | None = Field(None, validation_alias="responseDeadLine")
    place_of_performance: dict[str, Any] | None = Field(None, validation_alias="placeOfPerformance")
    active: bool = True
    description_url: str | None = Field(None, validation_alias="description")
    resource_links: list[str] = Field(default_factory=list, validation_alias="resourceLinks")
    ui_link: str | None = Field(None, validation_alias="uiLink")

    @field_validator("active", mode="before")
    @classmethod
    def _yes_no(cls, value: object) -> object:
        return value == "Yes" if isinstance(value, str) else value

    @field_validator("resource_links", mode="before")
    @classmethod
    def _null_list(cls, value: object) -> object:
        return [] if value is None else value


class SearchPage(BaseModel):
    total_records: int = Field(validation_alias="totalRecords")
    limit: int
    offset: int
    opportunities_data: list[dict[str, Any]] = Field(validation_alias="opportunitiesData")
