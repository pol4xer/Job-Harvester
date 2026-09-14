from __future__ import annotations

import html
import json
import re
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

from parsel import Selector

from job_harvester.config import SearchFilters

LINKEDIN_ORIGIN = "https://www.linkedin.com"
JOB_ID_FROM_URL = re.compile(r"/jobs/view/(?:[^/?#]*-)?(\d+)(?:[/?#]|$)")
JOB_ID_FROM_URN = re.compile(r"urn:li:jobPosting:(\d+)")

DATE_POSTED = {
    "past_24_hours": "r86400",
    "past_week": "r604800",
    "past_month": "r2592000",
}
EXPERIENCE_LEVELS = {
    "internship": "1",
    "entry": "2",
    "associate": "3",
    "mid_senior": "4",
    "director": "5",
    "executive": "6",
}
WORKPLACE_TYPES = {"on_site": "1", "remote": "2", "hybrid": "3"}
EMPLOYMENT_TYPES = {
    "full_time": "F",
    "part_time": "P",
    "contract": "C",
    "temporary": "T",
    "volunteer": "V",
    "internship": "I",
    "other": "O",
}


def build_linkedin_search_url(keywords: str, location: str, filters: SearchFilters) -> str:
    params: list[tuple[str, str]] = [("keywords", keywords), ("location", location)]
    if filters.date_posted != "any":
        params.append(("f_TPR", DATE_POSTED[filters.date_posted]))
    experience = [
        EXPERIENCE_LEVELS[item] for item in filters.experience_levels if item in EXPERIENCE_LEVELS
    ]
    workplace = [
        WORKPLACE_TYPES[item] for item in filters.workplace_types if item in WORKPLACE_TYPES
    ]
    employment = [
        EMPLOYMENT_TYPES[item] for item in filters.employment_types if item in EMPLOYMENT_TYPES
    ]
    if experience:
        params.append(("f_E", ",".join(experience)))
    if workplace:
        params.append(("f_WT", ",".join(workplace)))
    if employment:
        params.append(("f_JT", ",".join(employment)))
    params.append(("sortBy", "DD" if filters.sort_by == "recent" else "R"))
    return f"{LINKEDIN_ORIGIN}/jobs/search/?{urlencode(params)}"


def extract_linkedin_job_id(*values: str | None) -> str | None:
    for value in values:
        if not value:
            continue
        value = value.strip()
        if value.isdigit():
            return value
        urn_match = JOB_ID_FROM_URN.search(value)
        if urn_match:
            return urn_match.group(1)
        url_match = JOB_ID_FROM_URL.search(value)
        if url_match:
            return url_match.group(1)
        for key, query_value in parse_qsl(urlsplit(value).query):
            if key.casefold() == "currentjobid" and query_value.isdigit():
                return query_value
    return None


def canonical_linkedin_job_url(source_job_id: str | None, source_url: str) -> str:
    if source_job_id:
        return f"{LINKEDIN_ORIGIN}/jobs/view/{source_job_id}/"
    return urljoin(LINKEDIN_ORIGIN, source_url)


def clean_text(value: str | None) -> str:
    return " ".join((value or "").split())


def selector_text(selector) -> str:
    if selector is None:
        return ""
    return clean_text(selector.xpath("string(.)").get())


def first_text(response, selectors: Iterable[str]) -> str:
    for css in selectors:
        node = response.css(css)
        if node:
            text = clean_text(node.xpath("string(.)").get())
            if text:
                return text
    return ""


def first_attr(response, selectors: Iterable[str], attribute: str) -> str | None:
    for css in selectors:
        value = response.css(css).attrib.get(attribute) if response.css(css) else None
        if value:
            return value.strip()
    return None


def _json_ld_objects(value: Any):
    if isinstance(value, dict):
        yield value
        graph = value.get("@graph")
        if graph is not None:
            yield from _json_ld_objects(graph)
    elif isinstance(value, list):
        for item in value:
            yield from _json_ld_objects(item)


def extract_job_posting_json_ld(response) -> dict:
    for raw in response.css("script[type='application/ld+json']::text").getall():
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        for candidate in _json_ld_objects(payload):
            item_type = candidate.get("@type")
            if item_type == "JobPosting" or (
                isinstance(item_type, list) and "JobPosting" in item_type
            ):
                return candidate
    return {}


def html_to_text(value: str | None) -> str:
    if not value:
        return ""
    decoded = html.unescape(value)
    selector = Selector(text=decoded)
    parts = [
        clean_text(part)
        for part in selector.xpath(
            "//text()[not(ancestor::script) and not(ancestor::style) and not(ancestor::noscript)]"
        ).getall()
    ]
    return "\n".join(part for part in parts if part)


def parse_date(value: str | date | datetime | None) -> date | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    value = value.strip()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        pass

    lowered = value.casefold()
    number_match = re.search(r"(\d+)", lowered)
    count = int(number_match.group(1)) if number_match else 0
    today = datetime.now(UTC).date()
    if "hour" in lowered or "час" in lowered or "today" in lowered or "сегодня" in lowered:
        return today
    if "day" in lowered or "дн" in lowered:
        return today - timedelta(days=count or 1)
    if "week" in lowered or "нед" in lowered:
        return today - timedelta(days=7 * (count or 1))
    return None


def extract_location(job_posting: dict) -> str | None:
    locations = job_posting.get("jobLocation") or []
    if isinstance(locations, dict):
        locations = [locations]
    formatted: list[str] = []
    for location in locations:
        if not isinstance(location, dict):
            continue
        address = location.get("address") or {}
        if isinstance(address, str):
            formatted.append(clean_text(address))
            continue
        if not isinstance(address, dict):
            continue
        country = address.get("addressCountry")
        if isinstance(country, dict):
            country = country.get("name")
        parts = [
            address.get("addressLocality"),
            address.get("addressRegion"),
            country,
        ]
        text = ", ".join(clean_text(str(part)) for part in parts if part)
        if text:
            formatted.append(text)
    return "; ".join(dict.fromkeys(formatted)) or None


def normalize_employment_type(value: Any) -> str:
    if isinstance(value, list):
        value = value[0] if value else None
    normalized = clean_text(str(value or "")).replace("_", "-").casefold()
    mapping = {
        "full-time": "full-time",
        "full time": "full-time",
        "полный рабочий день": "full-time",
        "полная занятость": "full-time",
        "contractor": "contract",
        "contract": "contract",
        "контракт": "contract",
        "работа по контракту": "contract",
        "part-time": "part-time",
        "part time": "part-time",
        "неполный рабочий день": "part-time",
        "частичная занятость": "part-time",
        "temporary": "temporary",
        "временная работа": "temporary",
        "internship": "internship",
        "intern": "internship",
        "стажировка": "internship",
    }
    return mapping.get(normalized, "unknown")


def normalize_workplace_type(job_posting: dict, description: str) -> tuple[str, bool]:
    value = clean_text(str(job_posting.get("jobLocationType") or "")).casefold()
    if "telecommute" in value or value == "remote":
        return "remote", False
    lowered = description.casefold()
    if re.search(r"\bhybrid\b", lowered):
        return "hybrid", True
    if re.search(r"\b(on[- ]site|onsite)\b", lowered):
        return "on-site", True
    if re.search(r"\b(remote|work from home)\b", lowered):
        return "remote", True
    return "unknown", False


def parse_salary(job_posting: dict) -> dict:
    salary = job_posting.get("baseSalary") or {}
    if not isinstance(salary, dict):
        return {}
    currency = salary.get("currency")
    value = salary.get("value")
    if value is None:
        value = {}
    if not isinstance(value, dict):
        value = {"value": value}

    def decimal_or_none(item):
        try:
            number = Decimal(str(item)) if item is not None else None
            return number if number is not None and number.is_finite() else None
        except (InvalidOperation, ValueError):
            return None

    minimum = decimal_or_none(value.get("minValue", value.get("value")))
    maximum = decimal_or_none(value.get("maxValue", value.get("value")))
    unit = clean_text(str(value.get("unitText") or "")).casefold()
    interval = next((name for name in ("hour", "day", "month", "year") if name in unit), None)
    if minimum is None and maximum is None:
        return {}
    text_parts = [str(item) for item in (minimum, maximum) if item is not None]
    salary_text = "–".join(text_parts)
    if currency:
        salary_text = f"{salary_text} {currency}"
    if interval:
        salary_text = f"{salary_text}/{interval}"
    return {
        "salary_min": minimum,
        "salary_max": maximum,
        "salary_currency": currency,
        "salary_interval": interval,
        "salary_text": salary_text,
    }


def extract_criteria(response) -> dict[str, str]:
    output: dict[str, str] = {}
    for item in response.css(".description__job-criteria-item"):
        label = clean_text(item.css(".description__job-criteria-subheader::text").get())
        value = clean_text(item.css(".description__job-criteria-text").xpath("string(.)").get())
        if label and value:
            output[label] = value
    return output


def _criteria_value(criteria: dict[str, str], needles: Iterable[str]) -> str | None:
    for label, value in criteria.items():
        lowered = label.casefold()
        if any(needle in lowered for needle in needles):
            return value
    return None


def parse_job_detail(response, stub: dict) -> dict:
    job_posting = extract_job_posting_json_ld(response)
    criteria = extract_criteria(response)

    description_html = html.unescape(str(job_posting.get("description") or ""))
    if not description_html:
        for selector in (
            "[id^='JobDetails_AboutTheJob_']",
            ".description .show-more-less-html__markup",
            ".show-more-less-html__markup",
            ".jobs-description-content__text",
            ".jobs-description__content",
            "#job-details",
        ):
            node = response.css(selector)
            if node:
                description_html = node.get() or ""
                break
    description = html_to_text(description_html)

    organization = job_posting.get("hiringOrganization") or {}
    json_company = organization.get("name") if isinstance(organization, dict) else None
    company = clean_text(str(json_company or "")) or first_text(
        response,
        (
            ".topcard__org-name-link",
            ".top-card-layout__card .topcard__flavor a",
            ".job-details-jobs-unified-top-card__company-name",
            "[aria-label^='Company,']",
        ),
    )
    title = clean_text(str(job_posting.get("title") or "")) or first_text(
        response, ("h1.top-card-layout__title", "h1.topcard__title", "h1")
    )
    if not title:
        page_title = first_text(response, ("title",))
        title = clean_text(page_title.split(" | ", 1)[0]) if page_title else ""

    canonical_url = first_attr(response, ("link[rel='canonical']",), "href")
    canonical_url = canonical_url or first_attr(response, ("meta[property='og:url']",), "content")
    canonical_url = canonical_url or response.url
    urn = first_attr(
        response,
        ("[data-semaphore-content-urn^='urn:li:jobPosting:']",),
        "data-semaphore-content-urn",
    )
    source_job_id = extract_linkedin_job_id(
        urn,
        canonical_url,
        response.url,
        stub.get("source_job_id"),
    )
    source_url = canonical_linkedin_job_url(source_job_id, canonical_url)

    location = extract_location(job_posting) or first_text(
        response,
        (
            ".topcard__flavor-row .topcard__flavor--bullet",
            ".job-details-jobs-unified-top-card__primary-description-container",
        ),
    )
    posted_at = parse_date(job_posting.get("datePosted"))
    if posted_at is None:
        posted_raw = first_attr(response, ("time[datetime]",), "datetime") or first_text(
            response, (".posted-time-ago__text",)
        )
        posted_at = parse_date(posted_raw or stub.get("posted_at"))

    employment_raw = job_posting.get("employmentType") or _criteria_value(
        criteria, ("employment", "тип занятости")
    )
    employment_type = normalize_employment_type(employment_raw)
    workplace_type, workplace_inferred = normalize_workplace_type(job_posting, description)

    seniority = _criteria_value(criteria, ("seniority", "уровень должности", "должностной уровень"))
    job_function = _criteria_value(criteria, ("job function", "должностные обязанности"))
    industries = job_posting.get("industry") or _criteria_value(criteria, ("industr", "отрасл"))
    if isinstance(industries, list):
        industries = ", ".join(str(item) for item in industries)

    apply_url = None
    for selector in (
        "a.apply-button[href]",
        "a[data-tracking-control-name*='apply'][href]",
        "a.top-card-layout__cta[href]",
    ):
        candidate = first_attr(response, (selector,), "href")
        if candidate:
            resolved_candidate = urljoin(response.url, candidate)
            hostname = (urlsplit(resolved_candidate).hostname or "").casefold()
            if not (hostname == "linkedin.com" or hostname.endswith(".linkedin.com")):
                apply_url = resolved_candidate
                break

    applicants = first_text(response, (".num-applicants__caption",)) or None
    salary = parse_salary(job_posting)

    description_loaded = True
    if response.request is not None:
        description_loaded = response.meta.get("linkedin_description_loaded", True)
    capture_status = (
        "complete" if description_loaded and description and company and title else "partial"
    )
    result = {
        **stub,
        "source": "linkedin",
        "source_job_id": source_job_id,
        "source_url": source_url,
        "canonical_url": source_url,
        "apply_url": apply_url,
        "company": company or stub.get("company") or "Unknown company",
        "title": title or stub.get("title") or "Unknown role",
        "description": description or stub.get("description") or "",
        "description_html": description_html or None,
        "location": location or stub.get("location"),
        "workplace_type": workplace_type,
        "workplace_type_inferred": workplace_inferred,
        "employment_type": employment_type,
        "posted_at": posted_at,
        "valid_through": job_posting.get("validThrough"),
        "applicants": applicants,
        "seniority_level": seniority,
        "job_function": job_function,
        "industries": clean_text(str(industries or "")) or None,
        "criteria": criteria,
        "capture_status": capture_status,
        "captured_at": datetime.now(UTC),
        "raw_html": response.text,
        **salary,
    }
    return result
