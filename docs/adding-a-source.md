# Adding a source

Sources supply normalized vacancies. Storage, change tracking and export operate on the shared `Vacancy` contract and do not import adapters.

## A minimal adapter

Create `job_harvester/spiders/example_board.py`:

```python
import scrapy

from job_harvester.items import Vacancy


class ExampleBoardSpider(scrapy.Spider):
    name = "example_board"
    source = "example_board"

    async def start(self):
        # Replace this fictional record with parsing of your permitted API,
        # feed or website. Yield Vacancy dictionaries from the callbacks.
        yield Vacancy(
            source=self.source,
            source_job_id="example-101",
            source_url="https://example.com/jobs/example-101",
            company="Example Studio",
            title="Backend Engineer",
            description="A fictional vacancy demonstrating the adapter contract.",
            workplace_type="remote",
        ).as_dict()
```

Add a registration to `job_harvester/sources.py`:

```python
register_source(
    SourceDefinition(
        slug="example_board",
        description="Example board adapter",
        spider_path="job_harvester.spiders.example_board.ExampleBoardSpider",
    )
)
```

Use the existing commands:

```bash
uv run --frozen job-harvester crawl --source example_board --database data/example/jobs.sqlite3
uv run --frozen job-harvester export --database data/example/jobs.sqlite3 --format json --output data/example/jobs.json
```

Alternatively, call `register_source(...)` in your Python entry point before `run_crawl(source="example_board")`. Registration is process-local; there is no dynamic plugin discovery or command-line module loading. Call `run_crawl` once per process because Twisted's reactor cannot restart. Use separate CLI processes for successive runs.

## Adapter contract

- Match the spider's `source` to the registered slug. Slugs use lowercase letters, digits, hyphens or underscores. Existing registrations cannot be silently replaced.
- Yield `Vacancy(...).as_dict()`. Provide a stable `source_job_id` where available; otherwise identity uses the canonical URL. The model recomputes the source-scoped deduplication key and content hash.
- Use `capture_status="partial"` for incomplete details. Export selection preserves an earlier complete snapshot.
- A spider may expose `profile_id`, `query`, `location` and `run_metadata` for search provenance. Include only data appropriate for the local database. The pipeline creates a run ID if the spider does not provide one.
- The runner passes common search options through Scrapy's spider constructor. Handle additional arguments with `**kwargs`. For a file adapter, set `requires_input=True` and accept `input_path`; the runner checks that the file exists before starting.
- Browser-free adapters use ordinary Scrapy requests or local data. `requires_browser=True` enables the existing Selenium middleware, whose navigation logic is LinkedIn-specific. Another browser-driven website may need its own middleware or acquisition logic; the registry does not automatically adapt LinkedIn navigation to another site.
- Failed items, logged crawl errors or an unsuccessful close reason cause a nonzero CLI result. Successful earlier batches may remain stored; inspect `search_runs` for completion status.

The JSONL adapter streams local records, validates each one, strips supplied identity hashes and assigns its own source/run metadata. URLs in the file are not fetched.

## Verification

[`test_registering_a_custom_spider_needs_no_storage_or_export_changes`](../tests/test_sources.py) creates a third adapter in a temporary directory, registers it, runs the real Scrapy pipeline and exports its record. It makes no website requests.

For a real adapter, add handwritten parser fixtures, an end-to-end test against local input, and failure cases. Keep authenticated captures, session profiles and actual exports outside version control.
