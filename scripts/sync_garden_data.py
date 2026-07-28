"""
sync_garden_data.py

Pulls new HOBO logger .xlsx exports from the #tgif Slack channel, parses
them, and appends tidy records to data/garden/white_creek_temp.csv.
Tracks which files have already been processed in data/garden/manifest.json
so re-runs are idempotent (safe to run every Friday via GitHub Actions).

Requires:
    SLACK_BOT_TOKEN   env var, scopes: channels:history, files:read
    pip install slack_sdk openpyxl

Binary .xlsx content cannot be read through the Slack MCP tool used in
chat sessions -- this script, run with a real bot token (locally or in
GitHub Actions), is the only path that actually reads the file bytes.
Nothing here has been run against a real HOBO file yet. Test locally with
SLACK_BOT_TOKEN set before trusting the GitHub Actions run.
"""

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

    HOBO exports: row 1 = title, row 2 = header (units embedded in column
    name), data from row 3. Column position (datetime=col B, temp=col C)
    is more stable across exports than the exact header text -- but this
    is unverified against a real #tgif file. Adjust skiprows/columns after
    the first real run if the shape is different.
    """
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active

    rows = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i < 2:  # skip title + header rows
            continue
        if len(row) < 3 or row[1] is None or row[2] is None:
            continue
        dt_raw, temp_f = row[1], row[2]
        try:
            temp_f = float(temp_f)
        except (TypeError, ValueError):
            continue
        if isinstance(dt_raw, datetime):
            dt_iso = dt_raw.isoformat()
        else:
            continue  # unparsed datetime formats logged for manual review
        temp_c = (temp_f - 32) * 5 / 9
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

    save_manifest(manifest)
    print(f"Done. {new_total} new records synced.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
