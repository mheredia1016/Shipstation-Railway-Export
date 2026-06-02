import csv
import os
import sys
from base64 import b64encode
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

import paramiko
import requests

SHIPSTATION_API_BASE = os.getenv("SHIPSTATION_API_BASE", "https://ssapi.shipstation.com")
SHIPSTATION_API_KEY = os.getenv("SHIPSTATION_API_KEY", "").strip()
SHIPSTATION_API_SECRET = os.getenv("SHIPSTATION_API_SECRET", "").strip()

TIMEZONE = os.getenv("TIMEZONE", "America/Chicago")
DATE_MODE = os.getenv("DATE_MODE", "today").strip().lower()  # today or yesterday
STORE_ID = os.getenv("SHIPSTATION_STORE_ID", "").strip()
PAGE_SIZE = int(os.getenv("SHIPSTATION_PAGE_SIZE", "500"))

EXPORT_DIR = Path(os.getenv("EXPORT_DIR", "exports"))
CSV_PREFIX = os.getenv("CSV_PREFIX", "shipstation-shipped-orders")

SFTP_ENABLED = os.getenv("SFTP_ENABLED", os.getenv("FTP_ENABLED", "true")).lower() == "true"
SFTP_HOST = os.getenv("SFTP_HOST", os.getenv("FTP_HOST", "")).strip()
SFTP_PORT = int(os.getenv("SFTP_PORT", os.getenv("FTP_PORT", "22")))
SFTP_USERNAME = os.getenv("SFTP_USERNAME", os.getenv("FTP_USERNAME", "")).strip()
SFTP_PASSWORD = os.getenv("SFTP_PASSWORD", os.getenv("FTP_PASSWORD", "")).strip()
SFTP_REMOTE_DIR = os.getenv("SFTP_REMOTE_DIR", os.getenv("FTP_REMOTE_DIR", "/")).strip()
SFTP_UPLOAD_LATEST = os.getenv("SFTP_UPLOAD_LATEST", os.getenv("FTP_UPLOAD_LATEST", "true")).lower() == "true"


def require_env() -> None:
    missing = []
    if not SHIPSTATION_API_KEY:
        missing.append("SHIPSTATION_API_KEY")
    if not SHIPSTATION_API_SECRET:
        missing.append("SHIPSTATION_API_SECRET")
    if SFTP_ENABLED:
        if not SFTP_HOST:
            missing.append("SFTP_HOST")
        if not SFTP_USERNAME:
            missing.append("SFTP_USERNAME")
        if not SFTP_PASSWORD:
            missing.append("SFTP_PASSWORD")
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))


def auth_headers() -> Dict[str, str]:
    raw = f"{SHIPSTATION_API_KEY}:{SHIPSTATION_API_SECRET}".encode("utf-8")
    token = b64encode(raw).decode("utf-8")
    return {"Authorization": f"Basic {token}", "Accept": "application/json"}


def date_range_for_mode() -> tuple[str, str, str]:
    tz = ZoneInfo(TIMEZONE)
    now_local = datetime.now(tz)
    target = now_local.date() - timedelta(days=1) if DATE_MODE == "yesterday" else now_local.date()
    start_local = datetime.combine(target, time.min, tzinfo=tz)
    end_local = datetime.combine(target, time.max.replace(microsecond=0), tzinfo=tz)
    return start_local.isoformat(), end_local.isoformat(), target.strftime("%Y-%m-%d")


def get_shipments_page(page: int, start_date: str, end_date: str) -> Dict[str, Any]:
    url = f"{SHIPSTATION_API_BASE.rstrip('/')}/shipments"
    params = {
        "shipDateStart": start_date,
        "shipDateEnd": end_date,
        "page": page,
        "pageSize": PAGE_SIZE,
    }
    if STORE_ID:
        params["storeId"] = STORE_ID

    response = requests.get(url, headers=auth_headers(), params=params, timeout=60)
    response.raise_for_status()
    return response.json()


def fetch_all_shipments() -> tuple[List[Dict[str, Any]], str]:
    start_date, end_date, date_stamp = date_range_for_mode()
    print(f"Fetching ShipStation shipments from {start_date} to {end_date}")

    page = 1
    all_shipments: List[Dict[str, Any]] = []
    while True:
        payload = get_shipments_page(page, start_date, end_date)
        shipments = payload.get("shipments", [])
        if not isinstance(shipments, list):
            shipments = []
        print(f"Fetched page {page}: {len(shipments)} shipments")
        all_shipments.extend(shipments)
        total_pages = int(payload.get("pages") or 1)
        if page >= total_pages:
            break
        page += 1
    return all_shipments, date_stamp


def shipment_order_number(shipment: Dict[str, Any]) -> str:
    return str(shipment.get("orderNumber") or shipment.get("order_number") or shipment.get("orderKey") or "").strip()


def write_csv(shipments: List[Dict[str, Any]], date_stamp: str) -> Path:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = EXPORT_DIR / f"{CSV_PREFIX}-{date_stamp}.csv"
    seen = set()
    rows = []
    for shipment in shipments:
        order_number = shipment_order_number(shipment)
        if not order_number or order_number in seen:
            continue
        seen.add(order_number)
        rows.append({"Channel Reference Number": order_number})

    with output_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["Channel Reference Number"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} channel reference numbers to {output_path}")
    return output_path


def sftp_mkdirs(sftp: paramiko.SFTPClient, remote_dir: str) -> None:
    if not remote_dir or remote_dir == "/":
        sftp.chdir("/")
        return
    current = ""
    for part in remote_dir.strip("/").split("/"):
        current += "/" + part
        try:
            sftp.stat(current)
        except FileNotFoundError:
            sftp.mkdir(current)
    sftp.chdir(remote_dir)


def upload_file(local_path: Path) -> None:
    if not SFTP_ENABLED:
        print("SFTP upload disabled.")
        return

    transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
    transport.connect(username=SFTP_USERNAME, password=SFTP_PASSWORD)
    sftp = paramiko.SFTPClient.from_transport(transport)
    try:
        sftp_mkdirs(sftp, SFTP_REMOTE_DIR)
        remote_file = f"{SFTP_REMOTE_DIR.rstrip('/')}/{local_path.name}"
        sftp.put(str(local_path), remote_file)
        print(f"Uploaded {local_path.name} to {SFTP_REMOTE_DIR}")

        if SFTP_UPLOAD_LATEST:
            latest_name = f"{CSV_PREFIX}-latest.csv"
            latest_remote_file = f"{SFTP_REMOTE_DIR.rstrip('/')}/{latest_name}"
            sftp.put(str(local_path), latest_remote_file)
            print(f"Uploaded {latest_name} to {SFTP_REMOTE_DIR}")
    finally:
        sftp.close()
        transport.close()


def main() -> int:
    try:
        require_env()
        shipments, date_stamp = fetch_all_shipments()
        csv_path = write_csv(shipments, date_stamp)
        upload_file(csv_path)
        print("Done.")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
