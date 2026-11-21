from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from job_workflow.cli_extensions_snippet import run_notify_approved, run_sync_sheets


def test_notify_approved_sends_once_and_skips_duplicate(tmp_path: Path) -> None:
    digest = tmp_path / 'approved_digest.md'
    digest.write_text('# Approved Job Applications\n\n## Vanguard AI\n', encoding='utf-8')

    with patch('job_workflow.cli_extensions_snippet.send_approved_digest_email') as mock_send:
        first = run_notify_approved(tmp_path, recipient='getgad.uss@gmail.com')
        second = run_notify_approved(tmp_path, recipient='getgad.uss@gmail.com')

    assert first['status'] == 'sent'
    assert second['status'] == 'duplicate_skipped'
    assert mock_send.call_count == 1


def test_sync_sheets_maps_approved_packets(tmp_path: Path) -> None:
    packets = tmp_path / 'approved_packets.json'
    packets.write_text(
        json.dumps([
            {
                'company': 'Vanguard AI',
                'title': 'Applied AI Systems Engineer',
                'casefile_id': '33e4520f42180ecb009a991f',
                'location': 'Remote',
                'fit_score': 82.0,
                'fit_label': 'STRONG_APPLY',
                'url': 'https://boards.greenhouse.io/vanguard/jobs/901',
                'tailored_summary': 'Applied AI engineer',
                'resume_bullets': ['Bullet 1', 'Bullet 2'],
                'outreach_draft': 'Hello team',
            }
        ]),
        encoding='utf-8',
    )

    with patch('job_workflow.cli_extensions_snippet.sync_approved_rows_to_sheet') as mock_sync:
        result = run_sync_sheets(
            data_dir=tmp_path,
            credentials_path='creds.json',
            spreadsheet_id='sheet-123',
            worksheet_name='Sheet1',
        )

    assert result['status'] == 'synced'
    assert result['rows_synced'] == 1
    mock_sync.assert_called_once()
