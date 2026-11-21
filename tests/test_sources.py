import json

from job_workflow.limits import BudgetPolicy, RunBudgetTracker
from job_workflow.sources import GreenhouseBoardSource, LeverBoardSource, strip_html_to_text


def test_strip_html_to_text_decodes_entities_and_collapses_whitespace() -> None:
    html_snippet = "<p>Build <strong>Python</strong> &amp; <em>SQL</em>&nbsp;systems.</p>"
    assert strip_html_to_text(html_snippet) == "Build Python & SQL systems."


def test_greenhouse_source_parses_jobs_and_tracks_api_call() -> None:
    sample_payload = {
        "jobs": [
            {
                "id": 12345,
                "title": "Staff AI Workflow Engineer",
                "absolute_url": "https://boards.greenhouse.io/example/jobs/12345",
                "location": {"name": "Remote (US)"},
                "content": "<p>Design <b>durable</b> pipelines, retries, and evaluation suites.</p>",
            }
        ]
    }
    budget = RunBudgetTracker(policy=BudgetPolicy(max_api_calls_per_run=5))
    source = GreenhouseBoardSource(
        board_token="example",
        company_name="Example Corp",
        fetcher=lambda _url: json.dumps(sample_payload),
    )

    postings = source.fetch_postings(budget)
    assert len(postings) == 1
    assert postings[0].source == "greenhouse"
    assert postings[0].external_id == "12345"
    assert postings[0].company == "Example Corp"
    assert postings[0].location == "Remote (US)"
    assert "durable pipelines, retries, and evaluation suites." in postings[0].raw_description
    assert budget.api_calls_made == 1


def test_lever_source_parses_postings_and_sections() -> None:
    sample_payload = [
        {
            "id": "lv-777",
            "text": "Distributed Systems Engineer",
            "hostedUrl": "https://jobs.lever.co/acme/lv-777",
            "categories": {"location": "San Francisco, CA"},
            "descriptionPlain": "Own fault-tolerant ingestion and workflow state.",
            "lists": [
                {
                    "text": "Responsibilities",
                    "content": "<li>Maintain <strong>SQLite WAL</strong> state stores</li>",
                }
            ],
        }
    ]
    budget = RunBudgetTracker(policy=BudgetPolicy(max_api_calls_per_run=5))
    source = LeverBoardSource(
        site="acme",
        company_name="Acme Systems",
        fetcher=lambda _url: json.dumps(sample_payload),
    )

    postings = source.fetch_postings(budget)
    assert len(postings) == 1
    assert postings[0].source == "lever"
    assert postings[0].external_id == "lv-777"
    assert postings[0].company == "Acme Systems"
    assert postings[0].location == "San Francisco, CA"
    assert "Responsibilities:" in postings[0].raw_description
    assert "SQLite WAL state stores" in postings[0].raw_description
    assert budget.api_calls_made == 1
