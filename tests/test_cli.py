import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from job_workflow.cli import main
from job_workflow.schemas import JobPosting


def test_cli_run_pending_inspect_and_approve(tmp_path: Path, capsys) -> None:
    jobs_path = tmp_path / "jobs.json"
    data_dir = tmp_path / "data"

    jobs_payload = [
        {
            "source": "greenhouse",
            "company": "Helios Labs",
            "title": "AI Reliability Engineer",
            "canonical_url": "https://boards.greenhouse.io/helios/jobs/404",
            "raw_description": "Design resilient agent pipelines, structured output validators, and audit manifests.",
        }
    ]
    jobs_path.write_text(json.dumps(jobs_payload), encoding="utf-8")

    rc = main(["--data-dir", str(data_dir), "run-once", "--run-id", "cli-run-1", "--jobs-file", str(jobs_path)])
    assert rc == 0
    run_out = json.loads(capsys.readouterr().out)
    assert run_out["discovered_count"] == 1
    assert run_out["approvals_queued"] == 1

    rc = main(["--data-dir", str(data_dir), "pending"])
    assert rc == 0
    pending_out = json.loads(capsys.readouterr().out)
    assert len(pending_out) == 1
    casefile_id = pending_out[0]["casefile_id"]

    rc = main(["--data-dir", str(data_dir), "inspect", "--casefile-id", casefile_id])
    assert rc == 0
    artifact_out = json.loads(capsys.readouterr().out)
    assert artifact_out["state"] == "AWAITING_APPROVAL"

    rc = main([
        "--data-dir",
        str(data_dir),
        "approve",
        "--casefile-id",
        casefile_id,
        "--notes",
        "Approved via CLI test",
    ])
    assert rc == 0
    decide_out = json.loads(capsys.readouterr().out)
    assert decide_out["state"] == "APPROVED"


def test_cli_fetch_greenhouse_saves_and_runs_batch(tmp_path: Path, capsys) -> None:
    data_dir = tmp_path / "data"
    out_file = tmp_path / "fetched.json"
    mock_postings = [
        JobPosting(
            source="greenhouse",
            external_id="808",
            company="Demo AI",
            title="ML Infrastructure Engineer",
            location="Remote",
            canonical_url="https://boards.greenhouse.io/demo/jobs/808",  # type: ignore[arg-type]
            raw_description="Build durable ML pipelines, observability, and fault-tolerant workers.",
            discovered_at=datetime(2026, 11, 20, 18, 0, 0, tzinfo=timezone.utc),
        )
    ]

    with patch("job_workflow.sources.GreenhouseBoardSource.fetch_postings", return_value=mock_postings):
        rc = main([
            "--data-dir",
            str(data_dir),
            "fetch-greenhouse",
            "--board-token",
            "demo",
            "--company",
            "Demo AI",
            "--out-file",
            str(out_file),
            "--run-id",
            "gh-run-1",
        ])

    assert rc == 0
    assert out_file.exists()
    manifest_out = json.loads(capsys.readouterr().out)
    assert manifest_out["discovered_count"] == 1
    assert manifest_out["approvals_queued"] == 1


def test_cli_fetch_lever_runs_batch(tmp_path: Path, capsys) -> None:
    data_dir = tmp_path / "data"
    mock_postings = [
        JobPosting(
            source="lever",
            external_id="lv-909",
            company="Lever Demo",
            title="Agent Workflow Engineer",
            location="Remote",
            canonical_url="https://jobs.lever.co/demo/lv-909",  # type: ignore[arg-type]
            raw_description="Design deterministic agent workflows, retries, and structured evaluations.",
            discovered_at=datetime(2026, 11, 20, 18, 0, 0, tzinfo=timezone.utc),
        )
    ]

    with patch("job_workflow.sources.LeverBoardSource.fetch_postings", return_value=mock_postings):
        rc = main([
            "--data-dir",
            str(data_dir),
            "fetch-lever",
            "--site",
            "demo",
            "--company",
            "Lever Demo",
            "--run-id",
            "lv-run-1",
        ])

    assert rc == 0
    manifest_out = json.loads(capsys.readouterr().out)
    assert manifest_out["discovered_count"] == 1
    assert manifest_out["approvals_queued"] == 1


def test_cli_run_sources_combines_greenhouse_and_lever(tmp_path: Path, capsys) -> None:
    data_dir = tmp_path / "data"
    sources_file = tmp_path / "sources.json"
    sources_file.write_text(
        json.dumps([
            {"source": "greenhouse", "board_token": "gh-demo", "company": "GH Corp"},
            {"source": "lever", "site": "lv-demo", "company": "LV Corp"},
        ]),
        encoding="utf-8",
    )

    gh_posting = [
        JobPosting(
            source="greenhouse",
            external_id="101",
            company="GH Corp",
            title="Backend Systems Engineer",
            location="Remote",
            canonical_url="https://boards.greenhouse.io/gh/jobs/101",  # type: ignore[arg-type]
            raw_description="Build reliable distributed storage and workflow orchestration services.",
            discovered_at=datetime(2026, 11, 20, 18, 0, 0, tzinfo=timezone.utc),
        )
    ]
    lv_posting = [
        JobPosting(
            source="lever",
            external_id="202",
            company="LV Corp",
            title="Applied AI Engineer",
            location="Remote",
            canonical_url="https://jobs.lever.co/lv/202",  # type: ignore[arg-type]
            raw_description="Develop structured evaluation pipelines and resilient agent tooling.",
            discovered_at=datetime(2026, 11, 20, 18, 0, 0, tzinfo=timezone.utc),
        )
    ]

    with (
        patch("job_workflow.sources.GreenhouseBoardSource.fetch_postings", return_value=gh_posting),
        patch("job_workflow.sources.LeverBoardSource.fetch_postings", return_value=lv_posting),
    ):
        rc = main([
            "--data-dir",
            str(data_dir),
            "run-sources",
            "--run-id",
            "multi-run-1",
            "--sources-file",
            str(sources_file),
        ])

    assert rc == 0
    manifest_out = json.loads(capsys.readouterr().out)
    assert manifest_out["discovered_count"] == 2
    assert manifest_out["approvals_queued"] == 2

def test_cli_export_approved_json_and_md(tmp_path: Path, capsys) -> None:
    jobs_path = tmp_path / "jobs.json"
    data_dir = tmp_path / "data"

    jobs_payload = [
        {
            "source": "greenhouse",
            "company": "Approved Systems Co",
            "title": "Workflow Systems Engineer",
            "canonical_url": "https://boards.greenhouse.io/approved/jobs/100",
            "raw_description": "Durable state pipelines and schema validation.",
        }
    ]
    jobs_path.write_text(json.dumps(jobs_payload), encoding="utf-8")

    assert main(["--data-dir", str(data_dir), "run-once", "--run-id", "exp-run-1", "--jobs-file", str(jobs_path)]) == 0
    capsys.readouterr()

    assert main(["--data-dir", str(data_dir), "pending"]) == 0
    pending_items = json.loads(capsys.readouterr().out)
    assert len(pending_items) == 1
    cid = pending_items[0]["casefile_id"]

    assert main(["--data-dir", str(data_dir), "approve", "--casefile-id", cid, "--notes", "Looks great"]) == 0
    capsys.readouterr()

    out_json = tmp_path / "approved.json"
    assert main([
        "--data-dir", str(data_dir),
        "export-approved",
        "--format", "json",
        "--output", str(out_json),
    ]) == 0
    exported_data = json.loads(out_json.read_text(encoding="utf-8"))
    assert len(exported_data) == 1
    assert exported_data[0]["casefile_id"] == cid
    assert exported_data[0]["company"] == "Approved Systems Co"

    out_md = tmp_path / "approved.md"
    assert main([
        "--data-dir", str(data_dir),
        "export-approved",
        "--format", "md",
        "--output", str(out_md),
    ]) == 0
    md_text = out_md.read_text(encoding="utf-8")
    assert "Approved Systems Co - Workflow Systems Engineer" in md_text
    assert cid in md_text
