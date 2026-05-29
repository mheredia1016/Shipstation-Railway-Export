import csv
import os
import sys
from base64 import b64encode
from datetime import datetime, time, timedelta
from ftplib import FTP, FTP_TLS
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

import requests


SHIPSTATION_API_BASE = os.getenv("SHIPSTATION_API_BASE", "https://ssapi.shipstation.com")
SHIPSTATION_API_KEY = os.getenv("SHIPSTATION_API_KEY", "").strip()
SHIPSTATION_API_SECRET = os.getenv("SHIPSTATION_API_SECRET", "").strip()

TIMEZONE = os.getenv("TIMEZONE", "America/Chicago")
EXPORT_DIR = Path(os.getenv("EXPORT_DIR", "exports"))
CSV_PREFIX = os.getenv("CSV_PREFIX", "shipstation-shipped-orders")
PAGE_SIZE = int(os.getenv("SHIPSTATION_PAGE_SIZE", "500"))

# Optional filters
STORE_ID = os.getenv("SHIPSTATION_STORE_ID", "").strip()

# DATE_MODE:
# today = current local day
# yesterday = previous local day
DATE_MODE = os.getenv("DATE_MODE", "today").strip().lower()

# FTP settings
FTP_ENABLED = os.getenv("FTP_ENABLED", "true").lower() == "true"
FTP_PROTOCOL = os.getenv("FTP_PROTOCOL", "ftps").lower()  # ftp or ftps
FTP_HOST = os.getenv("FTP_HOST", "").strip()
FTP_PORT = int(os.getenv("FTP_PORT", "21"))
FTP_USERNAME = os.getenv("FTP_USERNAME", "").strip()
FTP_PASSWORD = os.getenv("FTP_PASSWORD", "").strip()
FTP_REMOTE_DIR = os.getenv("FTP_REMOTE_DIR", "/").strip()
FTP_UPLOAD_LATEST = os.getenv("FTP_UPLOAD_LATEST", "true").lower() == "true"


def require_env() -> None:
    missing = []

    if not SHIPSTATION_API_KEY:
        missing.append("SHIPSTATION_API_KEY")

    if not SHIPSTATION_API_SECRET:
        missing.append("SHIPSTATION_API_SECRET")

    if FTP_ENABLED:
        for key, value in {
            "FTP_HOST": FTP_HOST,
            "FTP_USERNAME": FTP_USERNAME,
            "FTP_PASSWORD": FTP_PASSWORD,
        }.items():
            if not value:
                missing.append(key)

    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))


def auth_headers() -> Dict[str, str]:
    raw = f"{SHIPSTATION_API_KEY}:{SHIPSTATION_API_SECRET}".encode("utf-8")
    token = b64encode(raw).decode("utf-8")

    return {
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
    }


def date_range_for_mode() -> tuple[str, str, str]:
    tz = ZoneInfo(TIMEZONE)
    now_local = datetime.now(tz)

    if DATE_MODE == "yesterday":
        target = now_local.date() - timedelta(days=1)
    else:
        target = now_local.date()

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

    response = requests.get(
        url,
        headers=auth_headers(),
        params=params,
        timeout=60,
    )

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
    return str(
        shipment.get("orderNumber")
        or shipment.get("order_number")
        or shipment.get("orderKey")
        or ""
    ).strip()


def write_csv(shipments: List[Dict[str, Any]], date_stamp: str) -> Path:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    output_path = EXPORT_DIR / f"{CSV_PREFIX}-{date_stamp}.csv"

    seen = set()
    rows = []

    for shipment in shipments:
        order_number = shipment_order_number(shipment)

        if not order_number:
            continue

        # Avoid duplicate rows if an order has multiple shipment records.
        if order_number in seen:
            continue

        seen.add(order_number)
        rows.append({"Order Number": order_number})

    with output_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["Order Number"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} order numbers to {output_path}")

    return output_path


def ftp_mkdirs(ftp: FTP, remote_dir: str) -> None:
    if not remote_dir or remote_dir == "/":
        ftp.cwd("/")
        return

    parts = [p for p in remote_dir.strip("/").split("/") if p]

    ftp.cwd("/")

    for part in parts:
        try:
            ftp.mkd(part)
        except Exception:
            pass

        ftp.cwd(part)


def upload_file(local_path: Path) -> None:
    if not FTP_ENABLED:
        print("FTP upload disabled.")
        return

    ftp_class = FTP_TLS if FTP_PROTOCOL == "ftps" else FTP
    ftp = ftp_class()

    ftp.connect(FTP_HOST, FTP_PORT, timeout=60)
    ftp.login(FTP_USERNAME, FTP_PASSWORD)

    if isinstance(ftp, FTP_TLS):
        ftp.prot_p()

    ftp_mkdirs(ftp, FTP_REMOTE_DIR)

    with local_path.open("rb") as f:
        ftp.storbinary(f"STOR {local_path.name}", f)

    print(f"Uploaded {local_path.name} to {FTP_REMOTE_DIR}")

    if FTP_UPLOAD_LATEST:
        latest_name = f"{CSV_PREFIX}-latest.csv"

        with local_path.open("rb") as f:
            ftp.storbinary(f"STOR {latest_name}", f)

        print(f"Uploaded {latest_name} to {FTP_REMOTE_DIR}")

    ftp.quit()


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
