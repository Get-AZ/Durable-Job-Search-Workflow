from __future__ import annotations

from typing import Any


def sync_approved_rows_to_sheet(
    credentials_path: str,
    spreadsheet_id: str,
    worksheet_name: str,
    rows: list[list[Any]],
) -> None:
    try:
        import gspread
        from gspread.utils import ValueInputOption
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "gspread is required for sync-sheets. Install it with: pip install gspread"
        ) from exc

    client = gspread.service_account(filename=credentials_path)
    sheet = client.open_by_key(spreadsheet_id).worksheet(worksheet_name)

    headers = [
        'Company',
        'Role',
        'Casefile ID',
        'Location',
        'Fit Score',
        'URL',
        'Tailored Summary',
        'Resume Bullets',
        'Outreach Draft',
        'Status',
    ]
    sheet.clear()
    sheet.update([headers, *rows], value_input_option=ValueInputOption.user_entered)
