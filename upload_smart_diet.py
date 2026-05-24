#!/usr/bin/env python3
"""Upload SmartDiet body composition data to Health Planet (innerscan)."""

import argparse
import csv
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Browser, Page, sync_playwright

LOGIN_URL = "https://www.healthplanet.jp/login.do"
INNERSCAN_URL = "https://www.healthplanet.jp/innerscan.do?date={date}"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def load_credentials() -> dict:
    path = Path("login_data/health_planet.json")
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def load_correction() -> dict:
    path = Path("correction.json")
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def load_sd_records(correction: dict) -> list[dict]:
    a = correction["a"]
    b = correction["b"]
    records: list[dict] = []
    seen: set[str] = set()

    for csv_path in sorted(Path("sddata").glob("*.csv")):
        log.info("Reading %s", csv_path)
        with csv_path.open(encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            next(reader)  # skip header
            for row in reader:
                if len(row) < 4:
                    continue
                date_str = row[0].strip()   # yyyy/MM/dd
                weight_str = row[2].strip()
                body_fat_str = row[3].strip()

                if not weight_str or not body_fat_str:
                    continue

                try:
                    weight_omron = float(weight_str)
                    body_fat = float(body_fat_str)
                except ValueError:
                    log.warning("Skipping invalid row: %s", row)
                    continue

                body_fat_tanita = a * body_fat + b
                date_compact = datetime.strptime(date_str, "%Y/%m/%d").strftime("%Y%m%d")

                if date_compact in seen:
                    continue
                seen.add(date_compact)

                records.append(
                    {
                        "date": date_compact,
                        "hh": "07",
                        "mm": "00",
                        "weight": round(weight_omron, 1),
                        "body_fat": round(body_fat_tanita, 1),
                    }
                )

    records.sort(key=lambda r: r["date"])
    return records


def login(page: Page, creds: dict) -> None:
    log.info("Logging in as '%s'", creds["id"])
    page.goto(LOGIN_URL)
    page.wait_for_load_state("domcontentloaded")
    page.fill('input[name="loginId"]', creds["id"])
    page.fill('input[name="passwd"]', creds["password"])
    with page.expect_navigation():
        page.evaluate(
            "document.forms[0].method='POST';"
            "document.forms[0].action='https://www.healthplanet.jp/login.do';"
            "document.forms[0].submit();"
        )
    page.wait_for_load_state("domcontentloaded")
    if "login" in page.url:
        raise RuntimeError(
            "Login failed — still on login page. "
            "Check credentials in login_data/health_planet.json"
        )
    log.info("Login complete — URL: %s", page.url)


def enter_record(page: Page, record: dict, dry_run: bool = False) -> bool:
    url = INNERSCAN_URL.format(date=record["date"])
    page.goto(url)
    page.wait_for_load_state("domcontentloaded")

    # Ensure the input form is visible (existing data may hide it via JS)
    page.evaluate(
        "var el = document.getElementById('input_form'); if (el) el.style.display = 'block';"
    )

    page.locator('input[name="measurementTimeHH"]').fill(record["hh"])
    page.locator('input[name="measurementTimeMM"]').fill(record["mm"])

    page.locator('input[name="innerscanBean[0].keyData"]').fill(str(record["weight"]))
    page.locator('input[name="innerscanBean[1].keyData"]').fill(str(record["body_fat"]))

    if dry_run:
        log.info(
            "[DRY RUN] %s %s:%s — weight=%.1f body_fat=%.1f",
            record["date"],
            record["hh"],
            record["mm"],
            record["weight"],
            record["body_fat"],
        )
        return True

    with page.expect_navigation():
        page.evaluate(
            "document.forms[0].method='POST';"
            "document.forms[0].action='/innerscan_confirm.do';"
            "document.forms[0].submit();"
        )
    page.wait_for_load_state("domcontentloaded")

    confirm = page.query_selector("a.btn_yes") or page.query_selector(
        'input[type="submit"]'
    )
    if confirm:
        with page.expect_navigation():
            page.evaluate(
                "document.forms[0].method='POST';"
                "document.forms[0].submit();"
            )
        page.wait_for_load_state("domcontentloaded")
        log.info("Registered — URL: %s", page.url)
        return True

    log.warning("No confirm button found on %s", page.url)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upload SmartDiet body composition data to Health Planet (innerscan)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show records without submitting"
    )
    parser.add_argument(
        "--headless", action="store_true", help="Run browser without GUI"
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.5,
        help="Seconds to wait between entries (default: 1.5)",
    )
    parser.add_argument(
        "--from-date", metavar="YYYYMMDD", help="Skip records before this date"
    )
    parser.add_argument(
        "--to-date", metavar="YYYYMMDD", help="Skip records after this date"
    )
    args = parser.parse_args()

    creds = load_credentials()
    correction = load_correction()
    records = load_sd_records(correction)

    if args.from_date:
        records = [r for r in records if r["date"] >= args.from_date]
    if args.to_date:
        records = [r for r in records if r["date"] <= args.to_date]

    if not records:
        log.info("No records to upload.")
        return

    log.info("Records to upload: %d", len(records))
    if args.dry_run:
        log.info("DRY RUN — no data will be submitted")

    failed: list[dict] = []

    with sync_playwright() as p:
        browser: Browser = p.chromium.launch(headless=args.headless)
        page = browser.new_page()
        try:
            login(page, creds)
            for i, record in enumerate(records, 1):
                log.info(
                    "[%d/%d] %s %s:%s weight=%.1f body_fat=%.1f",
                    i,
                    len(records),
                    record["date"],
                    record["hh"],
                    record["mm"],
                    record["weight"],
                    record["body_fat"],
                )
                try:
                    ok = enter_record(page, record, dry_run=args.dry_run)
                    if not ok:
                        failed.append(record)
                except Exception as exc:
                    log.error("Failed: %s — %s", record, exc)
                    failed.append(record)
                if args.delay > 0 and i < len(records):
                    time.sleep(args.delay)
        finally:
            browser.close()

    if failed:
        log.warning("%d record(s) failed:", len(failed))
        for r in failed:
            log.warning("  %s %s:%s", r["date"], r["hh"], r["mm"])
        sys.exit(1)
    else:
        log.info("All records uploaded successfully.")


if __name__ == "__main__":
    main()
