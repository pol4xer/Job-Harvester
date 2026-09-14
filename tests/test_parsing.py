"""Handwritten HTML and JSON-LD examples; no account or live pages required."""

import json
import unittest
from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

from scrapy import Request
from scrapy.http import HtmlResponse

from job_harvester.config import SearchFilters
from job_harvester.parsing import (
    build_linkedin_search_url,
    extract_job_posting_json_ld,
    html_to_text,
    parse_date,
    parse_job_detail,
    parse_salary,
)


def response(body, *, loaded=True):
    return HtmlResponse(
        url="https://www.linkedin.com/jobs/view/123/",
        body=body.encode(),
        encoding="utf-8",
        request=Request(
            "https://www.linkedin.com/jobs/view/123/", meta={"linkedin_description_loaded": loaded}
        ),
    )


class ParsingTests(unittest.TestCase):
    def test_job_posting_is_found_in_graph_after_invalid_script(self):
        posting = {"@type": ["Thing", "JobPosting"], "title": "Example Engineer"}
        page = response(
            '<script type="application/ld+json">not JSON</script>'
            '<script type="application/ld+json">'
            + json.dumps({"@graph": [{"@type": "Organization"}, posting]})
            + "</script>"
        )
        self.assertEqual(extract_job_posting_json_ld(page), posting)

    def test_json_ld_detail_extracts_job_contract(self):
        posting = {
            "@type": "JobPosting",
            "title": "Platform Engineer",
            "hiringOrganization": {"name": "Example Studio"},
            "description": "<p>Build APIs &amp; reliable tools.</p><script>ignored()</script>",
            "employmentType": "FULL_TIME",
            "jobLocationType": "TELECOMMUTE",
            "datePosted": "2026-09-01",
            "validThrough": "2026-10-01",
            "jobLocation": {
                "address": {
                    "addressLocality": "Example City",
                    "addressCountry": {"name": "Example Country"},
                }
            },
            "baseSalary": {
                "currency": "USD",
                "value": {"minValue": 80000, "maxValue": 100000, "unitText": "YEAR"},
            },
        }
        page = response(
            '<script type="application/ld+json">'
            + json.dumps(posting).replace("</", "<\\/")
            + '</script><a class="apply-button" '
            'href="https://careers.example.com/apply/123">Apply</a>'
        )
        result = parse_job_detail(page, {})
        self.assertEqual(result["source_job_id"], "123")
        self.assertEqual(result["company"], "Example Studio")
        self.assertEqual(result["description"], "Build APIs & reliable tools.")
        self.assertEqual(result["capture_status"], "complete")
        self.assertEqual(result["workplace_type"], "remote")
        self.assertFalse(result["workplace_type_inferred"])
        self.assertEqual(result["employment_type"], "full-time")
        self.assertEqual(result["posted_at"], date(2026, 9, 1))
        self.assertEqual(result["salary_min"], Decimal("80000"))
        self.assertEqual(result["salary_max"], Decimal("100000"))
        self.assertEqual(result["salary_interval"], "year")
        self.assertEqual(result["location"], "Example City, Example Country")
        self.assertEqual(result["apply_url"], "https://careers.example.com/apply/123")

    def test_html_detail_fallback_keeps_parsed_stub_date(self):
        page = response(
            '<h1 class="top-card-layout__title">Example Role</h1>'
            '<a class="topcard__org-name-link">Example Company</a>'
            '<div class="show-more-less-html__markup">Hybrid role. Build tools.</div>'
        )
        result = parse_job_detail(page, {"posted_at": date(2026, 8, 31)})
        self.assertEqual(result["capture_status"], "complete")
        self.assertEqual(result["posted_at"], date(2026, 8, 31))
        self.assertEqual(result["workplace_type"], "hybrid")
        self.assertTrue(result["workplace_type_inferred"])

    def test_missing_or_unloaded_description_stays_partial(self):
        for page in [
            response("<h1>Example role</h1>"),
            response(
                '<h1>Example Role</h1><a class="topcard__org-name-link">Example Co</a>'
                '<div class="show-more-less-html__markup">Preview text only.</div>',
                loaded=False,
            ),
        ]:
            with self.subTest(page=page.text):
                result = parse_job_detail(page, {"company": "Card Company"})
                self.assertEqual(result["capture_status"], "partial")

    def test_external_apply_domain_is_not_mistaken_for_linkedin(self):
        page = response(
            '<a class="apply-button" href="https://notlinkedin.com/apply/123">Apply</a>'
        )
        self.assertEqual(
            parse_job_detail(page, {})["apply_url"], "https://notlinkedin.com/apply/123"
        )

    def test_html_to_text_omits_script_and_style(self):
        self.assertEqual(
            html_to_text(
                "<p>Hello &amp; welcome.</p><style>hidden</style>"
                "<script>hidden()</script><p>Second paragraph.</p>"
            ),
            "Hello & welcome.\nSecond paragraph.",
        )

    def test_invalid_salary_numbers_are_not_exported(self):
        self.assertEqual(parse_salary({"baseSalary": {"value": "NaN"}}), {})
        self.assertEqual(parse_salary({"baseSalary": {"value": "Infinity"}}), {})
        self.assertEqual(parse_salary({"baseSalary": {"value": 0}})["salary_min"], Decimal(0))

    def test_dates_and_search_filters(self):
        self.assertIsNone(parse_date("unknown"))
        self.assertEqual(parse_date("2026-09-01T12:30:00Z"), date(2026, 9, 1))
        params = parse_qs(
            urlsplit(
                build_linkedin_search_url("Python & APIs", "Example City", SearchFilters())
            ).query
        )
        self.assertEqual(params["keywords"], ["Python & APIs"])
        self.assertEqual(params["location"], ["Example City"])
        self.assertEqual(params["f_WT"], ["2"])
        self.assertEqual(params["sortBy"], ["DD"])
