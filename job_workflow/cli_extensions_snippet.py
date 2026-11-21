from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from job_workflow.notifier import send_approved_digest_email
from job_workflow.sheets_sync import sync_approved_rows_to_sheet


def _digest_hash(content: str) -> str:
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def run_notify_approved(
    data_dir: Path,
    recipient: str,
    force: bool = False,
) -> dict[str, object]:
    digest_path = data_dir / 'approved_digest.md'
    if not digest_path.exists():
        raise FileNotFoundError(f'Missing digest file: {digest_path}. Run export-approved first.')

    body_markdown = digest_path.read_text(encoding='utf-8')
    current_hash = _digest_hash(body_markdown)
    state_path = data_dir / 'notification_state.json'

    previous_state: dict[str, object] = {}
    if state_path.exists():
        loaded = json.loads(state_path.read_text(encoding='utf-8'))
        if isinstance(loaded, dict):
            previous_state = loaded

    if (
        not force
        and previous_state.get('last_sent_hash') == current_hash
        and previous_state.get('recipient') == recipient
    ):
        return {
            'status': 'duplicate_skipped',
            'recipient': recipient,
            'digest_sha256': current_hash,
        }

    send_approved_digest_email(
        subject='Approved Job Applications Digest',
        body_markdown=body_markdown,
        recipient=recipient,
    )
    state_path.write_text(
        json.dumps(
            {'recipient': recipient, 'last_sent_hash': current_hash},
            indent=2,
        )
        + '\n',
        encoding='utf-8',
    )
    return {
        'status': 'sent',
        'recipient': recipient,
        'digest_sha256': current_hash,
    }


def packets_to_sheet_rows(packets_path: Path) -> list[list[str]]:
    payload = json.loads(packets_path.read_text(encoding='utf-8'))
    packets = payload if isinstance(payload, list) else payload.get('packets', [])
    rows: list[list[str]] = []
    for pkt in packets:
        if not isinstance(pkt, dict):
            continue
        bullets = pkt.get('resume_bullets', [])
        bullets_str = '; '.join(str(b) for b in bullets) if isinstance(bullets, list) else str(bullets)
        score_val = pkt.get('fit_score')
        label_val = pkt.get('fit_label')
        fit_str = (
            f'{score_val} ({label_val})'
            if score_val is not None and label_val
            else str(score_val or 'None (N/A)')
        )
        rows.append([
            str(pkt.get('company', '')),
            str(pkt.get('title', pkt.get('role', ''))),
            str(pkt.get('casefile_id', '')),
            str(pkt.get('location', '')),
            fit_str,
            str(pkt.get('url', '')),
            str(pkt.get('tailored_summary', '')),
            bullets_str,
            str(pkt.get('outreach_draft', '')),
            'Approved - Ready to Submit',
        ])
    return rows


def run_sync_sheets(
    data_dir: Path,
    credentials_path: str,
    spreadsheet_id: str,
    worksheet_name: str,
) -> dict[str, object]:
    packets_path = data_dir / 'approved_packets.json'
    if not packets_path.exists():
        raise FileNotFoundError(f'Missing packets file: {packets_path}. Run export-approved first.')

    rows = packets_to_sheet_rows(packets_path)
    sync_approved_rows_to_sheet(
        credentials_path=credentials_path,
        spreadsheet_id=spreadsheet_id,
        worksheet_name=worksheet_name,
        rows=rows,
    )
    return {
        'status': 'synced',
        'worksheet': worksheet_name,
        'rows_synced': len(rows),
    }


def register_extension_subcommands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    notify_parser = subparsers.add_parser(
        'notify-approved',
        help='Send approved job digest via email',
    )
    notify_parser.add_argument(
        '--to',
        default=os.environ.get('NOTIFY_EMAIL_TO', 'getgad.uss@gmail.com'),
        help='Recipient email address',
    )
    notify_parser.add_argument(
        '--force',
        action='store_true',
        help='Send even if digest hash matches last sent email',
    )

    sheets_parser = subparsers.add_parser(
        'sync-sheets',
        help='Sync approved application packets to Google Sheets',
    )
    sheets_parser.add_argument(
        '--credentials',
        default=os.environ.get('GOOGLE_SHEETS_CREDENTIALS', ''),
        help='Path to Google service account JSON credentials',
    )
    sheets_parser.add_argument(
        '--spreadsheet-id',
        default=os.environ.get('GOOGLE_SHEETS_ID', ''),
        help='Target Google Sheets spreadsheet ID',
    )
    sheets_parser.add_argument(
        '--worksheet',
        default=os.environ.get('GOOGLE_SHEETS_WORKSHEET', 'Sheet1'),
        help='Target worksheet name',
    )


def dispatch_extension_command(args: argparse.Namespace, data_dir: Path) -> bool:
    if args.command == 'notify-approved':
        result = run_notify_approved(data_dir=data_dir, recipient=args.to, force=args.force)
        print(json.dumps(result, indent=2))
        return True
    if args.command == 'sync-sheets':
        if not args.credentials or not args.spreadsheet_id:
            print(
                'Missing --credentials / GOOGLE_SHEETS_CREDENTIALS or --spreadsheet-id / GOOGLE_SHEETS_ID',
                file=sys.stderr,
            )
            raise SystemExit(2)
        result = run_sync_sheets(
            data_dir=data_dir,
            credentials_path=args.credentials,
            spreadsheet_id=args.spreadsheet_id,
            worksheet_name=args.worksheet,
        )
        print(json.dumps(result, indent=2))
        return True
    return False
