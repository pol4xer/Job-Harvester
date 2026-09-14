from __future__ import annotations

from scrapy import Request

SELENIUM_META_KEY = "selenium_browser"
SELENIUM_PAGE_KIND_META_KEY = "selenium_page_kind"


class SeleniumBrowserRequest(Request):
    """A Scrapy request rendered by the project's SeleniumBase middleware."""

    def __init__(self, *args, page_kind: str = "detail", **kwargs):
        meta = dict(kwargs.pop("meta", None) or {})
        meta.setdefault(SELENIUM_META_KEY, True)
        meta.setdefault(SELENIUM_PAGE_KIND_META_KEY, page_kind)
        kwargs["meta"] = meta
        super().__init__(*args, **kwargs)
