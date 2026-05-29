# ShipStation Daily Shipped Orders Export for Railway

This pulls ShipStation shipments for the selected day, creates a one-column CSV, and uploads it to FTP/FTPS.

CSV format:

```csv
Order Number
12345
12346
```

## Railway start command

```bash
python export_shipstation_shipped_orders.py
```

## Required Railway variables

```text
SHIPSTATION_API_KEY=your_shipstation_api_key
SHIPSTATION_API_SECRET=your_shipstation_api_secret

FTP_ENABLED=true
FTP_PROTOCOL=ftps
FTP_HOST=your_ftp_host
FTP_PORT=21
FTP_USERNAME=your_ftp_username
FTP_PASSWORD=your_ftp_password
FTP_REMOTE_DIR=/linnworks/uploads
```

## Optional Railway variables

```text
TIMEZONE=America/Chicago
DATE_MODE=today
CSV_PREFIX=shipstation-shipped-orders
SHIPSTATION_PAGE_SIZE=500
SHIPSTATION_STORE_ID=
FTP_UPLOAD_LATEST=true
```

## Daily schedule

Recommended Railway cron:

```cron
59 23 * * *
```

That runs at 11:59 PM daily if Railway is using local time. If Railway uses UTC cron, convert Chicago time to UTC.

## Output files

```text
shipstation-shipped-orders-YYYY-MM-DD.csv
shipstation-shipped-orders-latest.csv
```
