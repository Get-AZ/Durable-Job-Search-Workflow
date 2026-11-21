from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from typing import Callable, Protocol
from urllib.request import Request, urlopen

from job_workflow.limits import RunBudgetTracker
from job_workflow.schemas import JobPosting

HttpFetcher = Callable[[str], str]

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def strip_html_to_text(raw_html: str) -> str:
    """Converts HTML job descriptions into normalized plain text."""
    unescaped = html.unescape(raw_html)
    without_tags = _TAG_RE.sub(" ", unescaped)
    return _WHITESPACE_RE.sub(" ", without_tags).strip()


def default_http_get(url: str) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": "DurableJobSearchWorkflow/0.1 (+https://localhost)",
            "Accept": "application/json",
        },
    )
    with urlopen(req, timeout=20) as resp:  # noqa: S310
        return resp.read().decode("utf-8")


class JobSourceAdapter(Protocol):
    def fetch_postings(self, budget: RunBudgetTracker) -> list[JobPosting]:
        """Fetches and normalizes job postings while respecting run budget limits."""


class GreenhouseBoardSource:
    """Fetches public job listings from Greenhouse's board JSON API."""

    def __init__(
        self,
        *,
        board_token: str,
        company_name: str,
        fetcher: HttpFetcher = default_http_get,
        include_content: bool = True,
    ) -> None:
        self.board_token = board_token.strip()
        self.company_name = company_name.strip()
        self.fetcher = fetcher
        self.include_content = include_content

    def build_url(self) -> str:
        content_param = "?content=true" if self.include_content else ""
        return f"https://boards-api.greenhouse.io/v1/boards/{self.board_token}/jobs{content_param}"

    def fetch_postings(self, budget: RunBudgetTracker) -> list[JobPosting]:
        budget.consume_api_call(count=1)
        try:
            raw_body = self.fetcher(self.build_url())
            payload = json.loads(raw_body)
            budget.record_success()
        except Exception:
            budget.record_failure()
            raise

        jobs_raw = payload.get("jobs", []) if isinstance(payload, dict) else []
        postings: list[JobPosting] = []
        now = datetime.now(timezone.utc)

        for item in jobs_raw:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            url = str(item.get("absolute_url", "")).strip()
            if not title or not url:
                continue

            location_obj = item.get("location")
            location_name = (
                str(location_obj.get("name", "Remote")).strip()
                if isinstance(location_obj, dict)
                else "Remote"
            ) or "Remote"

            raw_content = str(item.get("content", "") or "")
            cleaned_desc = strip_html_to_text(raw_content)
            if len(cleaned_desc) < 20:
                cleaned_desc = (
                    f"{title} opening at {self.company_name} ({location_name}). "
                    f"Full details available at {url}."
                )

            ext_id_val = item.get("id")
            external_id = str(ext_id_val) if ext_id_val is not None else None

            postings.append(
                JobPosting(
                    source="greenhouse",
                    external_id=external_id,
                    company=self.company_name,
                    title=title,
                    location=location_name,
                    canonical_url=url,  # type: ignore[arg-type]
                    raw_description=cleaned_desc,
                    discovered_at=now,
                )
            )

        return postings


def _extract_lever_description(item: dict[str, object], title: str, company: str, location: str, url: str) -> str:
    parts: list[str] = []
    plain = str(item.get("descriptionPlain") or "").strip()
    if plain:
        parts.append(plain)
    else:
        html_desc = str(item.get("description") or "").strip()
        if html_desc:
            parts.append(strip_html_to_text(html_desc))

    lists_raw = item.get("lists")
    if isinstance(lists_raw, list):
        for section in lists_raw:
            if not isinstance(section, dict):
                continue
            heading = str(section.get("text") or "").strip()
            section_text = strip_html_to_text(str(section.get("content") or ""))
            if heading and section_text:
                parts.append(f"{heading}: {section_text}")
            elif section_text:
                parts.append(section_text)

    combined = _WHITESPACE_RE.sub(" ", " ".join(parts)).strip()
    if len(combined) < 20:
        return f"{title} opening at {company} ({location}). Full details available at {url}."
    return combined


class LeverBoardSource:
    """Fetches public job postings from Lever's postings JSON API."""

    def __init__(
        self,
        *,
        site: str,
        company_name: str,
        fetcher: HttpFetcher = default_http_get,
    ) -> None:
        self.site = site.strip()
        self.company_name = company_name.strip()
        self.fetcher = fetcher

    def build_url(self) -> str:
        return f"https://api.lever.co/v0/postings/{self.site}?mode=json"

    def fetch_postings(self, budget: RunBudgetTracker) -> list[JobPosting]:
        budget.consume_api_call(count=1)
        try:
            raw_body = self.fetcher(self.build_url())
            payload = json.loads(raw_body)
            budget.record_success()
        except Exception:
            budget.record_failure()
            raise

        items = payload if isinstance(payload, list) else []
        postings: list[JobPosting] = []
        now = datetime.now(timezone.utc)

        for item in items:
            if not isinstance(item, dict):
                continue
            title = str(item.get("text") or "").strip()
            url = str(item.get("hostedUrl") or item.get("applyUrl") or "").strip()
            if not title or not url:
                continue

            categories = item.get("categories")
            location_name = (
                str(categories.get("location") or "Remote").strip()
                if isinstance(categories, dict)
                else "Remote"
            ) or "Remote"

            description = _extract_lever_description(
                item,
                title=title,
                company=self.company_name,
                location=location_name,
                url=url,
            )

            raw_id = item.get("id")
            external_id = str(raw_id).strip() if raw_id is not None and str(raw_id).strip() else None

            postings.append(
                JobPosting(
                    source="lever",
                    external_id=external_id,
                    company=self.company_name,
                    title=title,
                    location=location_name,
                    canonical_url=url,  # type: ignore[arg-type]
                    raw_description=description,
                    discovered_at=now,
                )
            )

        return postings
