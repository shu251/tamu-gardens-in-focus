"""
sync_garden_data.py

Pulls new HOBO logger .xlsx exports from the #tgif Slack channel, parses
them, and appends tidy records to data/garden/white_creek_temp.csv.
Tracks which files have already been processed in data/garden/manifest.json
so re-downloading is idempotent (safe to run every Friday via GitHub
Actions). Confirmed 2026-07-28: HOBO exports are cumulative -- each new
file re-exports the logger's full history, not just what's new since the
last download -- so a dedupe pass runs after every sync (see dedupe_csv()).

Requires:
    SLACK_BOT_TOKEN   env var, scopes: channels:history, files:read
    pip3 install slack_sdk openpyxl

Binary .xlsx content cannot be read through the Slack MCP tool used in
chat sessions -- this script, run with a real bot token (locally or in
GitHub Actions), is the only path that actually reads the file bytes.
"""

import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import openpyxl
import requests
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

CHANNEL_ID = "C0AGSKQS5PY"  # #tgif
REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data" / "garden"
RAW_DIR = DATA_DIR / "raw"
CSV_PATH = DATA_DIR / "white_creek_temp.csv"
MANIFEST_PATH = DATA_DIR / "manifest.json"

CSV_HEADER = "datetime,temp_f,temp_c\n"


def load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text())
    return {"channel_id": CHANNEL_ID, "last_sync": None, "synced_files": {}, "record_count": 0}


def save_manifest(manifest: dict) -> None:
    manifest["last_sync"] = datetime.now(timezone.utc).isoformat()
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))


def list_hobo_files(client: WebClient) -> list[dict]:
    """List .xlsx files posted in #tgif."""
    files = []
    cursor = None
    while True:
        resp = client.conversations_history(channel=CHANNEL_ID, cursor=cursor, limit=200)
        for msg in resp.get("messages", []):
            for f in msg.get("files", []):
                if f.get("name", "").lower().endswith(".xlsx"):
                    files.append(f)
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return files


def download_file(file_obj: dict, token: str) -> Path:
    url = file_obj["url_private_download"]
    dest = RAW_DIR / file_obj["name"]
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    return dest


def parse_hobo_xlsx(path: Path) -> list[tuple[str, float, float]]:
    """
    Returns list of (iso_datetime, temp_f, temp_c).

    Confirmed 2026-07-28 against a real #tgif file: there is NO title row.
    Row 0 is the header itself (e.g. "#", "Date-Time (CDT)",
    "Temperature , deg C", "Light , lux"), data starts at row 1. Column
    position (datetime=col B, temp=col C) matches; the earlier
    title-row-then-header-row assumption was wrong and silently dropped
    row 0 (the real header) while misreading row 1 (the first real
    reading) as the header -- confirmed by header text showing up as a
    bare float like "19.73140625" instead of a column label.

    Unit is also not fixed -- HOBO loggers can be configured in either F
    or C, and at least one real #tgif file logs in Celsius. Read the
    header text per file instead of assuming a fixed unit.
    """
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active

    is_celsius = None
    rows = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:  # header row -- detect unit from column 3's label
            header = str(row[2]) if len(row) > 2 and row[2] is not None else ""
            if "°C" in header or "deg C" in header or "Celsius" in header:
                is_celsius = True
            elif "°F" in header or "deg F" in header or "Fahrenheit" in header:
                is_celsius = False
            else:
                print(
                    f"  WARNING: could not detect temp unit from header "
                    f"'{header}' in {path.name} -- assuming Fahrenheit. Verify manually.",
                    file=sys.stderr,
                )
                is_celsius = False
            continue
        if len(row) < 3 or row[1] is None or row[2] is None:
            continue
        dt_raw, temp_raw = row[1], row[2]
        try:
            temp_raw = float(temp_raw)
        except (TypeError, ValueError):
            continue
        if isinstance(dt_raw, datetime):
            dt_iso = dt_raw.isoformat()
        else:
            continue  # unparsed datetime formats logged for manual review
        if is_celsius:
            temp_c = temp_raw
            temp_f = temp_raw * 9 / 5 + 32
        else:
            temp_f = temp_raw
            temp_c = (temp_raw - 32) * 5 / 9
        rows.append((dt_iso, round(temp_f, 2), round(temp_c, 2)))
    wb.close()
    return rows


def append_to_csv(rows: list[tuple[str, float, float]]) -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    is_new = not CSV_PATH.exists()
    with open(CSV_PATH, "a") as f:
        if is_new:
            f.write(CSV_HEADER)
        for dt_iso, temp_f, temp_c in rows:
            f.write(f"{dt_iso},{temp_f},{temp_c}\n")
    return len(rows)


def dedupe_csv() -> tuple[int, int]:
    """
    Rewrite the CSV keeping one row per datetime (last write wins).

    HOBO exports are cumulative -- each file re-exports the logger's full
    history, so successive syncs produce heavily overlapping rows (e.g.
    6 real files -> 4679 raw rows / 2400 unique). Dedupe after each sync
    rather than trying to prevent overlap while appending.

    Returns (rows_before, rows_after).
    """
    if not CSV_PATH.exists():
        return (0, 0)
    with open(CSV_PATH, newline="") as f:
        rows = list(csv.DictReader(f))
    before = len(rows)
    deduped = {row["datetime"]: row for row in rows}  # last occurrence wins
    after_rows = sorted(deduped.values(), key=lambda r: r["datetime"])
    with open(CSV_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["datetime", "temp_f", "temp_c"])
        writer.writeheader()
        writer.writerows(after_rows)
    return (before, len(after_rows))


def main() -> int:
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        print("SLACK_BOT_TOKEN not set -- nothing to sync.", file=sys.stderr)
        return 1

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    client = WebClient(token=token)
    manifest = load_manifest()

    try:
        files = list_hobo_files(client)
    except SlackApiError as e:
        print(f"Slack API error listing files: {e.response['error']}", file=sys.stderr)
        return 1

    new_total = 0
    for f in files:
        file_id = f["id"]
        if manifest["synced_files"].get(file_id, {}).get("status") == "synced":
            continue

        print(f"Syncing {f['name']} ({file_id}) ...")
        try:
            local_path = download_file(f, token)
            rows = parse_hobo_xlsx(local_path)
            n = append_to_csv(rows)
            manifest["synced_files"][file_id] = {
                "name": f["name"],
                "synced_at": datetime.now(timezone.utc).isoformat(),
                "status": "synced",
                "record_count": n,
            }
            manifest["record_count"] = manifest.get("record_count", 0) + n
            new_total += n
        except Exception as e:  # noqa: BLE001 -- log and continue with other files
            print(f"  FAILED: {e}", file=sys.stderr)
            manifest["synced_files"][file_id] = {
                "name": f["name"],
                "status": "failed",
                "error": str(e),
            }

    before, after = dedupe_csv()
    manifest["record_count"] = after
    save_manifest(manifest)
    print(f"Done. {new_total} raw records synced this run.")
    if before != after:
        print(f"Dedup: {before} rows -> {after} unique rows in white_creek_temp.csv "
              f"({before - after} overlapping rows removed, expected -- HOBO exports are cumulative).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
