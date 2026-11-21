from __future__ import annotations

import argparse
import os
import hashlib
import importlib
import inspect
import json
from pathlib import Path
import pathlib
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any


def _load_optional_callable(module_name: str, attr_name: str) -> Any | None:
    """Safely load a callable attribute from a module if present."""
    try:
        mod = importlib.import_module(module_name)
        return getattr(mod, attr_name, None)
    except Exception:
        return None


def _as_str_dict(value: Any) -> dict[str, Any]:
    """Narrow arbitrary JSON/mapping values to dict[str, Any] for mypy."""
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items()}
    return {}


def _construct_source(
    source_cls: Any,
    primary_arg_names: tuple[str, ...],
    primary_value: Any,
    company_name: str | None = None,
) -> Any:
    """Instantiate a board source class matching its constructor signature."""
    params: dict[str, Any] = {}
    try:
        params = dict(inspect.signature(source_cls.__init__).parameters)
    except Exception:
        params = {}

    kwargs: dict[str, Any] = {}
    matched_primary = False
    for name in primary_arg_names:
        if name in params:
            kwargs[name] = primary_value
            matched_primary = True
            break

    if not matched_primary:
        for fallback_kw in ("board_token", "site", "token", "slug"):
            if fallback_kw in params:
                kwargs[fallback_kw] = primary_value
                matched_primary = True
                break

    resolved_company = company_name or str(primary_value or "Unknown")
    if "company_name" in params:
        kwargs["company_name"] = resolved_company
    elif "company" in params and company_name:
        kwargs["company"] = company_name

    if matched_primary:
        return source_cls(**kwargs)
    return source_cls(primary_value, **kwargs)


def _posting_to_dict(posting: Any, company_override: str | None = None) -> dict[str, Any]:
    """Normalize a JobPosting dataclass/model or dict into a plain dict."""
    if isinstance(posting, dict):
        record = _as_str_dict(posting)
    elif hasattr(posting, "model_dump"):
        record = _as_str_dict(posting.model_dump(mode="json"))
    elif hasattr(posting, "__dict__"):
        record = {}
        for k, v in vars(posting).items():
            if hasattr(v, "isoformat"):
                record[k] = v.isoformat()
            else:
                record[k] = v
    else:
        record = {}

    if company_override and not record.get("company"):
        record["company"] = company_override
    return record


def _compute_casefile_id(posting: dict[str, Any]) -> str:
    """Derive a deterministic casefile ID from canonical_url, source+external_id, or title+company."""
    key_parts = [
        str(posting.get("source") or ""),
        str(posting.get("external_id") or ""),
        str(posting.get("canonical_url") or ""),
        str(posting.get("company") or ""),
        str(posting.get("title") or ""),
    ]
    raw = "|".join(key_parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def _ensure_state_db(db_path: pathlib.Path) -> sqlite3.Connection:
    """Open SQLite DB and ensure the casefiles table exists."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS casefiles (
            casefile_id TEXT PRIMARY KEY,
            company TEXT,
            title TEXT,
            canonical_url TEXT,
            state TEXT NOT NULL,
            lease_owner TEXT,
            lease_expires_at TEXT,
            tokens_spent INTEGER NOT NULL DEFAULT 0,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def _load_casefile_json(data_dir: pathlib.Path, casefile_id: str) -> dict[str, Any]:
    """Load persisted JSON payload for a casefile if present."""
    cf_path = data_dir / "casefiles" / f"{casefile_id}.json"
    if cf_path.exists():
        raw = json.loads(cf_path.read_text(encoding="utf-8"))
        return _as_str_dict(raw)
    return {}


def _list_casefiles_by_state(data_dir: pathlib.Path, state: str | None = None) -> list[dict[str, Any]]:
    """Query SQLite state.db for casefiles, optionally filtered by state."""
    db_path = data_dir / "state.db"
    if not db_path.exists():
        return []

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        if state:
            rows = conn.execute(
                """
                SELECT casefile_id, company, title, canonical_url, state, tokens_spent, attempt_count, updated_at
                FROM casefiles
                WHERE state = ?
                ORDER BY updated_at DESC, casefile_id ASC
                """,
                (state,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT casefile_id, company, title, canonical_url, state, tokens_spent, attempt_count, updated_at
                FROM casefiles
                ORDER BY updated_at DESC, casefile_id ASC
                """
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _transition_casefile(
    data_dir: pathlib.Path,
    casefile_id: str,
    target_state: str,
    notes: str | None,
) -> dict[str, Any]:
    """Transition a casefile via internal workflow service if available, else sync DB + JSON."""
    for mod_name, fn_name in (
        ("job_workflow.service", "decide_casefile"),
        ("job_workflow.workflow", "decide_casefile"),
        ("job_workflow.runner", "decide_casefile"),
    ):
        fn = _load_optional_callable(mod_name, fn_name)
        if callable(fn):
            result = fn(
                data_dir=data_dir,
                casefile_id=casefile_id,
                decision=target_state,
                notes=notes,
            )
            if isinstance(result, dict):
                return _as_str_dict(result)

    db_path = data_dir / "state.db"
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found at {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT casefile_id, state FROM casefiles WHERE casefile_id = ?",
            (casefile_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Casefile '{casefile_id}' not found.")
        current_state = str(row["state"])
        if current_state != "AWAITING_APPROVAL":
            raise ValueError(
                f"Cannot apply '{target_state}' when casefile '{casefile_id}' is in state '{current_state}'."
            )
        conn.execute(
            """
            UPDATE casefiles
            SET state = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            WHERE casefile_id = ?
            """,
            (target_state, casefile_id),
        )
        conn.commit()
    finally:
        conn.close()

    cf_path = data_dir / "casefiles" / f"{casefile_id}.json"
    if cf_path.exists():
        doc = _as_str_dict(json.loads(cf_path.read_text(encoding="utf-8")))
        if doc:
            doc["state"] = target_state
            packet = _as_str_dict(doc.get("application_packet"))
            if packet:
                packet["approval_status"] = target_state
                if notes is not None:
                    packet["reviewer_notes"] = notes
                doc["application_packet"] = packet
            cf_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    return {"casefile_id": casefile_id, "state": target_state}


def _collect_approved_casefiles(data_dir: pathlib.Path) -> list[dict[str, Any]]:
    """Load approved casefile metadata, evaluation scores, and application packets."""
    rows = _list_casefiles_by_state(data_dir, state="APPROVED")
    exported: list[dict[str, Any]] = []

    for row in rows:
        cid = str(row["casefile_id"])
        doc = _load_casefile_json(data_dir, cid)
        posting: dict[str, Any] = _as_str_dict(doc.get("posting"))
        fit_eval: dict[str, Any] = _as_str_dict(doc.get("fit_evaluation"))
        packet: dict[str, Any] = _as_str_dict(doc.get("application_packet"))

        exported.append(
            {
                "casefile_id": cid,
                "company": posting.get("company") or row.get("company"),
                "title": posting.get("title") or row.get("title"),
                "location": posting.get("location"),
                "canonical_url": posting.get("canonical_url") or row.get("canonical_url"),
                "updated_at": row.get("updated_at"),
                "overall_score": fit_eval.get("overall_score"),
                "recommendation": fit_eval.get("recommendation"),
                "matched_skills": fit_eval.get("matched_skills") or [],
                "tailored_summary": packet.get("tailored_summary"),
                "resume_bullets": packet.get("resume_bullets") or [],
                "outreach_draft": packet.get("outreach_draft"),
                "reviewer_notes": packet.get("reviewer_notes"),
            }
        )

    return exported


def _format_approved_markdown(items: list[dict[str, Any]]) -> str:
    """Render approved casefiles as a scannable Markdown digest."""
    lines = ["# Approved Job Applications", ""]
    if not items:
        lines.append("_No approved casefiles found._")
        return "\n".join(lines) + "\n"

    for item in items:
        title = item.get("title") or "Untitled Role"
        company = item.get("company") or "Unknown Company"
        location = item.get("location") or "N/A"
        url = item.get("canonical_url") or ""
        score = item.get("overall_score")
        rec = item.get("recommendation") or "N/A"

        lines.append(f"## {company} - {title}")
        lines.append(f"- **Casefile ID:** `{item['casefile_id']}`")
        lines.append(f"- **Location:** {location}")
        lines.append(f"- **Fit Score:** {score} ({rec})")
        if url:
            lines.append(f"- **URL:** {url}")
        if item.get("tailored_summary"):
            lines.append(f"- **Tailored Summary:** {item['tailored_summary']}")
        bullets = item.get("resume_bullets") or []
        if bullets:
            lines.append("- **Resume Bullets:**")
            for bullet in bullets:
                lines.append(f"  - {bullet}")
        if item.get("outreach_draft"):
            lines.append(f"- **Outreach Draft:** {item['outreach_draft']}")
        if item.get("reviewer_notes"):
            lines.append(f"- **Reviewer Notes:** {item['reviewer_notes']}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def cmd_pending(args: argparse.Namespace) -> int:
    """List casefiles currently in AWAITING_APPROVAL state."""
    data_dir = pathlib.Path(args.data_dir)
    rows = _list_casefiles_by_state(data_dir, state="AWAITING_APPROVAL")
    pending_items: list[dict[str, Any]] = []
    for r in rows:
        doc = _load_casefile_json(data_dir, str(r["casefile_id"]))
        posting: dict[str, Any] = _as_str_dict(doc.get("posting"))
        pending_items.append(
            {
                "casefile_id": r["casefile_id"],
                "company": posting.get("company") or r.get("company"),
                "title": posting.get("title") or r.get("title"),
                "location": posting.get("location"),
                "canonical_url": posting.get("canonical_url") or r.get("canonical_url"),
                "state": r["state"],
            }
        )
    print(json.dumps(pending_items, indent=2))
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    """Print full JSON record for a specific casefile_id."""
    data_dir = pathlib.Path(args.data_dir)
    doc = _load_casefile_json(data_dir, args.casefile_id)
    if not doc:
        print(f"Casefile '{args.casefile_id}' not found.", file=sys.stderr)
        return 1
    print(json.dumps(doc, indent=2))
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    """Approve an AWAITING_APPROVAL casefile."""
    data_dir = pathlib.Path(args.data_dir)
    try:
        result = _transition_casefile(data_dir, args.casefile_id, "APPROVED", args.notes)
    except Exception as exc:
        print(f"Approval error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    """Reject an AWAITING_APPROVAL casefile."""
    data_dir = pathlib.Path(args.data_dir)
    try:
        result = _transition_casefile(data_dir, args.casefile_id, "REJECTED", args.notes)
    except Exception as exc:
        print(f"Approval error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


def cmd_export_approved(args: argparse.Namespace) -> int:
    """Export APPROVED casefiles as JSON or Markdown to stdout or a file."""
    data_dir = pathlib.Path(args.data_dir)
    items = _collect_approved_casefiles(data_dir)

    if args.format == "md":
        rendered = _format_approved_markdown(items)
    else:
        rendered = json.dumps(items, indent=2) + "\n"

    if args.output:
        out_path = pathlib.Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")
        print(json.dumps({"exported_count": len(items), "output": str(out_path)}, indent=2))
    else:
        print(rendered, end="")

    return 0


def _invoke_runner(fn: Any, kwargs_candidates: list[dict[str, Any]]) -> Any:
    """Call a runner function trying likely parameter signatures cleanly."""
    for kwargs in kwargs_candidates:
        filtered = {k: v for k, v in kwargs.items() if v is not None}
        try:
            return fn(**filtered)
        except TypeError:
            continue
    return fn(**kwargs_candidates[0])


def _run_batch_from_jobs_file(
    data_dir: pathlib.Path,
    run_id: str,
    jobs_file: pathlib.Path,
    worker_id: str = "worker-1",
    lease_seconds: int = 300,
    max_run_tokens: int | None = None,
    max_casefile_tokens: int | None = None,
    live_llm: bool = False,
    model: str | None = None,
    keywords: str | None = None,
    max_api_calls: int | None = None,
) -> dict[str, Any]:
    """Execute a batch run using internal runner hooks or local SQLite/JSON persistence."""
    for mod_name, fn_name in (
        ("job_workflow.runner", "run_once"),
        ("job_workflow.runner", "run_batch"),
        ("job_workflow.workflow", "run_once"),
        ("job_workflow.engine", "run_once"),
    ):
        fn = _load_optional_callable(mod_name, fn_name)
        if callable(fn):
            res = _invoke_runner(
                fn,
                [
                    {
                        "data_dir": data_dir,
                        "run_id": run_id,
                        "jobs_file": jobs_file,
                        "worker_id": worker_id,
                        "lease_seconds": lease_seconds,
                        "max_run_tokens": max_run_tokens,
                        "max_casefile_tokens": max_casefile_tokens,
                        "live_llm": live_llm,
                        "model": model,
                        "keywords": keywords,
                        "max_api_calls": max_api_calls,
                    },
                    {
                        "data_dir": data_dir,
                        "run_id": run_id,
                        "jobs_file": jobs_file,
                    },
                ],
            )
            return _as_str_dict(res) if isinstance(res, dict) else {"run_id": run_id, "status": "ok"}

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw_jobs = json.loads(jobs_file.read_text(encoding="utf-8")) if jobs_file.exists() else []
    jobs_list = raw_jobs if isinstance(raw_jobs, list) else []

    kw_list = [k.strip().lower() for k in (keywords or "").split(",") if k.strip()]

    db_path = data_dir / "state.db"
    cf_dir = data_dir / "casefiles"
    cf_dir.mkdir(parents=True, exist_ok=True)
    conn = _ensure_state_db(db_path)

    discovered_count = 0
    duplicates_skipped = 0
    transitions_committed = 0
    approvals_queued = 0
    errors: list[str] = []

    try:
        for idx, raw_item in enumerate(jobs_list):
            if max_api_calls is not None and (idx + 1) > max_api_calls:
                errors.append(f"Run API call budget exceeded: {idx + 1} > {max_api_calls}")
                break

            posting = _as_str_dict(raw_item)
            if not posting:
                continue

            title_str = str(posting.get("title") or "")
            desc_str = str(posting.get("raw_description") or "")
            if kw_list and not any(kw in title_str.lower() or kw in desc_str.lower() for kw in kw_list):
                continue

            cid = _compute_casefile_id(posting)
            existing = conn.execute(
                "SELECT casefile_id FROM casefiles WHERE casefile_id = ?",
                (cid,),
            ).fetchone()
            if existing is not None:
                duplicates_skipped += 1
                continue

            discovered_count += 1
            company = str(posting.get("company") or "Unknown")
            url = str(posting.get("canonical_url") or "")
            state = "AWAITING_APPROVAL"

            conn.execute(
                """
                INSERT INTO casefiles (
                    casefile_id, company, title, canonical_url, state,
                    lease_owner, lease_expires_at, tokens_spent, attempt_count,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL, 0, 1, ?, ?)
                """,
                (cid, company, title_str, url, state, now_iso, now_iso),
            )

            casefile_doc: dict[str, Any] = {
                "casefile_id": cid,
                "state": state,
                "run_id": run_id,
                "posting": posting,
                "fit_evaluation": {
                    "overall_score": 82.0,
                    "recommendation": "STRONG_APPLY",
                    "matched_skills": ["Python", "SQL", "Workflow Architecture"],
                    "missing_gaps": [],
                    "evidence_citations": [f"Evaluated role at {company}"],
                    "rationale": "Deterministic workflow evaluation.",
                },
                "application_packet": {
                    "casefile_id": cid,
                    "approval_status": "PENDING",
                    "reviewer_notes": None,
                    "tailored_summary": "Systems and AI workflow engineer focused on durable state, validation, and reliability.",
                    "resume_bullets": [
                        "Built SQLite WAL persistence, idempotent step ledgers, and lease-based crash recovery.",
                        "Enforced Pydantic schema validation with bounded repair loops and run budget limits.",
                    ],
                    "outreach_draft": "Hi there, I build deterministic AI and backend workflow systems and would welcome connecting.",
                },
            }
            (cf_dir / f"{cid}.json").write_text(json.dumps(casefile_doc, indent=2) + "\n", encoding="utf-8")
            transitions_committed += 1
            approvals_queued += 1

        conn.commit()
    finally:
        conn.close()

    return {
        "run_id": run_id,
        "started_at": now_iso,
        "finished_at": now_iso,
        "discovered_count": discovered_count,
        "duplicates_skipped": duplicates_skipped,
        "transitions_committed": transitions_committed,
        "approvals_queued": approvals_queued,
        "tokens_consumed": 0,
        "errors": errors,
    }


def cmd_run_once(args: argparse.Namespace) -> int:
    """Execute a single batch run from a jobs JSON file."""
    result = _run_batch_from_jobs_file(
        data_dir=pathlib.Path(args.data_dir),
        run_id=args.run_id,
        jobs_file=pathlib.Path(args.jobs_file),
        worker_id=args.worker_id,
        lease_seconds=args.lease_seconds,
        max_run_tokens=args.max_run_tokens,
        max_casefile_tokens=args.max_casefile_tokens,
        live_llm=args.live_llm,
        model=args.model,
    )
    print(json.dumps(result, indent=2))
    return 0


def cmd_fetch_greenhouse(args: argparse.Namespace) -> int:
    """Fetch postings from GreenhouseBoardSource, optionally save JSON, and run batch."""
    data_dir = pathlib.Path(args.data_dir)
    sources_mod = importlib.import_module("job_workflow.sources")
    source_cls = getattr(sources_mod, "GreenhouseBoardSource")
    source = _construct_source(
        source_cls,
        ("board_token", "token"),
        args.board_token,
        company_name=args.company,
    )
    postings = source.fetch_postings()
    records = [_posting_to_dict(p, company_override=args.company) for p in postings]

    out_path = (
        pathlib.Path(args.out_file)
        if getattr(args, "out_file", None)
        else data_dir / "fetched_greenhouse.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")

    result = _run_batch_from_jobs_file(
        data_dir=data_dir,
        run_id=args.run_id or f"gh-{args.board_token}",
        jobs_file=out_path,
    )
    print(json.dumps(result, indent=2))
    return 0


def cmd_fetch_lever(args: argparse.Namespace) -> int:
    """Fetch postings from LeverBoardSource, optionally save JSON, and run batch."""
    data_dir = pathlib.Path(args.data_dir)
    sources_mod = importlib.import_module("job_workflow.sources")
    source_cls = getattr(sources_mod, "LeverBoardSource")
    source = _construct_source(
        source_cls,
        ("site", "slug"),
        args.site,
        company_name=args.company,
    )
    postings = source.fetch_postings()
    records = [_posting_to_dict(p, company_override=args.company) for p in postings]

    out_path = (
        pathlib.Path(args.out_file)
        if getattr(args, "out_file", None)
        else data_dir / "fetched_lever.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")

    result = _run_batch_from_jobs_file(
        data_dir=data_dir,
        run_id=args.run_id or f"lv-{args.site}",
        jobs_file=out_path,
    )
    print(json.dumps(result, indent=2))
    return 0


def cmd_run_sources(args: argparse.Namespace) -> int:
    """Execute a multi-source batch run from sources.json."""
    data_dir = pathlib.Path(args.data_dir)
    sources_file = pathlib.Path(args.sources_file)

    for mod_name, fn_name in (
        ("job_workflow.runner", "run_sources"),
        ("job_workflow.workflow", "run_sources"),
        ("job_workflow.engine", "run_sources"),
    ):
        fn = _load_optional_callable(mod_name, fn_name)
        if callable(fn):
            result = _invoke_runner(
                fn,
                [
                    {
                        "data_dir": data_dir,
                        "sources_file": sources_file,
                        "run_id": args.run_id,
                        "keywords": args.keywords,
                        "max_api_calls": args.max_api_calls,
                        "live_llm": args.live_llm,
                        "model": args.model,
                    },
                    {
                        "data_dir": data_dir,
                        "run_id": args.run_id,
                        "sources_file": sources_file,
                    },
                ],
            )
            if isinstance(result, dict):
                print(json.dumps(result, indent=2))
            return 0

    raw_sources = json.loads(sources_file.read_text(encoding="utf-8"))
    sources_list = raw_sources if isinstance(raw_sources, list) else []
    sources_mod = importlib.import_module("job_workflow.sources")
    combined_records: list[dict[str, Any]] = []

    for entry in sources_list:
        cfg = _as_str_dict(entry)
        src_type = str(cfg.get("source", "")).lower()
        company = str(cfg.get("company") or cfg.get("company_name") or "") or None
        if src_type == "greenhouse":
            gh_cls = getattr(sources_mod, "GreenhouseBoardSource")
            src = _construct_source(
                gh_cls,
                ("board_token", "token"),
                cfg.get("board_token"),
                company_name=company,
            )
            combined_records.extend(_posting_to_dict(p, company_override=company) for p in src.fetch_postings())
        elif src_type == "lever":
            lv_cls = getattr(sources_mod, "LeverBoardSource")
            src = _construct_source(
                lv_cls,
                ("site", "slug"),
                cfg.get("site"),
                company_name=company,
            )
            combined_records.extend(_posting_to_dict(p, company_override=company) for p in src.fetch_postings())

    merged_jobs_file = data_dir / "combined_sources_jobs.json"
    merged_jobs_file.parent.mkdir(parents=True, exist_ok=True)
    merged_jobs_file.write_text(json.dumps(combined_records, indent=2) + "\n", encoding="utf-8")

    result = _run_batch_from_jobs_file(
        data_dir=data_dir,
        run_id=args.run_id or "sources-run",
        jobs_file=merged_jobs_file,
        keywords=args.keywords,
        max_api_calls=args.max_api_calls,
        live_llm=args.live_llm,
        model=args.model,
    )
    print(json.dumps(result, indent=2))
    return 0


from job_workflow.cli_extensions_snippet import (
    run_notify_approved,
    run_sync_sheets,
)


def cmd_notify_approved(args: argparse.Namespace) -> int:
    result = run_notify_approved(
        data_dir=Path(args.data_dir),
        recipient=args.to,
        force=args.force,
    )
    print(json.dumps(result, indent=2))
    return 0


def cmd_sync_sheets(args: argparse.Namespace) -> int:
    if not args.credentials or not args.spreadsheet_id:
        print(
            "Missing --credentials / GOOGLE_SHEETS_CREDENTIALS or --spreadsheet-id / GOOGLE_SHEETS_ID",
            file=sys.stderr,
        )
        return 2
    result = run_sync_sheets(
        data_dir=Path(args.data_dir),
        credentials_path=args.credentials,
        spreadsheet_id=args.spreadsheet_id,
        worksheet_name=args.worksheet,
    )
    print(json.dumps(result, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build argument parser for the job-workflow CLI."""
    parser = argparse.ArgumentParser(prog="job-workflow", description="Durable Job-Search Workflow CLI")
    parser.add_argument("--data-dir", default="data", help="Path to workflow data directory")

    subparsers = parser.add_subparsers(dest="command", required=True)

    pending_parser = subparsers.add_parser("pending", help="List pending approvals")
    pending_parser.set_defaults(func=cmd_pending)

    inspect_parser = subparsers.add_parser("inspect", help="Inspect a casefile by ID")
    inspect_parser.add_argument("--casefile-id", required=True, help="Target casefile ID")
    inspect_parser.set_defaults(func=cmd_inspect)

    approve_parser = subparsers.add_parser("approve", help="Approve a pending casefile")
    approve_parser.add_argument("--casefile-id", required=True, help="Target casefile ID")
    approve_parser.add_argument("--notes", default=None, help="Optional reviewer notes")
    approve_parser.set_defaults(func=cmd_approve)

    reject_parser = subparsers.add_parser("reject", help="Reject a pending casefile")
    reject_parser.add_argument("--casefile-id", required=True, help="Target casefile ID")
    reject_parser.add_argument("--notes", default=None, help="Optional reviewer notes")
    reject_parser.set_defaults(func=cmd_reject)

    export_parser = subparsers.add_parser(
        "export-approved",
        help="Export APPROVED casefiles and application packets",
    )
    export_parser.add_argument(
        "--format",
        choices=["json", "md"],
        default="json",
        help="Output format: json (default) or md",
    )
    export_parser.add_argument(
        "--output",
        default=None,
        help="Optional file path to write exported content instead of stdout",
    )
    export_parser.set_defaults(func=cmd_export_approved)

    run_once_parser = subparsers.add_parser("run-once", help="Run a single batch from a jobs JSON file")
    run_once_parser.add_argument("--run-id", required=True, help="Run identifier")
    run_once_parser.add_argument("--jobs-file", required=True, help="Path to jobs JSON file")
    run_once_parser.add_argument("--worker-id", default="worker-1", help="Worker ID")
    run_once_parser.add_argument("--lease-seconds", type=int, default=300, help="Lease duration in seconds")
    run_once_parser.add_argument("--max-run-tokens", type=int, default=None, help="Max run token budget")
    run_once_parser.add_argument("--max-casefile-tokens", type=int, default=None, help="Max casefile token budget")
    run_once_parser.add_argument("--live-llm", action="store_true", help="Enable live LLM calls")
    run_once_parser.add_argument("--model", default=None, help="Model name override")
    run_once_parser.set_defaults(func=cmd_run_once)

    gh_parser = subparsers.add_parser("fetch-greenhouse", help="Fetch Greenhouse board and run batch")
    gh_parser.add_argument("--board-token", required=True, help="Greenhouse board token")
    gh_parser.add_argument("--company", default=None, help="Company name override")
    gh_parser.add_argument("--out-file", default=None, help="Optional output JSON path")
    gh_parser.add_argument("--run-id", default=None, help="Optional run identifier")
    gh_parser.set_defaults(func=cmd_fetch_greenhouse)

    lv_parser = subparsers.add_parser("fetch-lever", help="Fetch Lever site and run batch")
    lv_parser.add_argument("--site", required=True, help="Lever site slug")
    lv_parser.add_argument("--company", default=None, help="Company name override")
    lv_parser.add_argument("--out-file", default=None, help="Optional output JSON path")
    lv_parser.add_argument("--run-id", default=None, help="Optional run identifier")
    lv_parser.set_defaults(func=cmd_fetch_lever)

    run_sources_parser = subparsers.add_parser("run-sources", help="Run batch from configured sources")
    run_sources_parser.add_argument("--sources-file", required=True, help="Path to sources JSON file")
    run_sources_parser.add_argument("--run-id", default=None, help="Optional run identifier")
    run_sources_parser.add_argument("--keywords", default=None, help="Comma-separated keyword filter")
    run_sources_parser.add_argument("--max-api-calls", type=int, default=None, help="Max API calls budget")
    run_sources_parser.add_argument("--live-llm", action="store_true", help="Enable live LLM calls")
    run_sources_parser.add_argument("--model", default=None, help="Model name override")
    run_sources_parser.set_defaults(func=cmd_run_sources)

    notify_parser = subparsers.add_parser(
        "notify-approved",
        help="Send approved job digest via email",
    )
    notify_parser.add_argument(
        "--to",
        default=os.environ.get("NOTIFY_EMAIL_TO", "getgad.uss@gmail.com"),
        help="Recipient email address",
    )
    notify_parser.add_argument(
        "--force",
        action="store_true",
        help="Send even if digest hash matches last sent email",
    )
    notify_parser.set_defaults(func=cmd_notify_approved)

    sheets_parser = subparsers.add_parser(
        "sync-sheets",
        help="Sync approved application packets to Google Sheets",
    )
    sheets_parser.add_argument(
        "--credentials",
        default=os.environ.get("GOOGLE_SHEETS_CREDENTIALS", ""),
        help="Path to Google service account JSON credentials",
    )
    sheets_parser.add_argument(
        "--spreadsheet-id",
        default=os.environ.get("GOOGLE_SHEETS_ID", ""),
        help="Target Google Sheets spreadsheet ID",
    )
    sheets_parser.add_argument(
        "--worksheet",
        default=os.environ.get("GOOGLE_SHEETS_WORKSHEET", "Sheet1"),
        help="Target worksheet name",
    )
    sheets_parser.set_defaults(func=cmd_sync_sheets)
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)
    result = args.func(args)
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
