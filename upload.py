#!/usr/bin/env python3
"""Upload blood pressure data from bptracker SQLite databases to Health Planet."""

import argparse
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

from playwright.sync_api import Browser, Page, sync_playwright

LOGIN_URL = "https://www.healthplanet.jp/login.do"
BP_URL = "https://www.healthplanet.jp/bloodpressure.do?date={date}"

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


def load_bp_records() -> list[dict]:
    records: list[dict] = []
    seen: set[tuple] = set()
    for db_path in sorted(Path("bpdata").glob("*.db")):
        log.info("Reading %s", db_path)
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT tranxDate, tranxTime, sys, dia, pulse
            FROM tranx
            WHERE sys > 0 OR dia > 0 OR pulse > 0
            ORDER BY tranxDate, tranxTime
            """
        )
        for tranx_date, tranx_time, sys_val, dia_val, pulse_val in cur.fetchall():
            key = (tranx_date, tranx_time)
            if key in seen:
                continue
            seen.add(key)
            parts = tranx_time.split(":")
            hh, mm = parts[0].zfill(2), parts[1].zfill(2)
            records.append(
                {
                    "date": tranx_date.replace("-", ""),  # YYYYMMDD
                    "hh": hh,
                    "mm": mm,
                    "sys": sys_val,
                    "dia": dia_val,
                    "pulse": pulse_val,
                }
            )
        conn.close()

    records.sort(key=lambda r: (r["date"], r["hh"], r["mm"]))
    return records


def login(page: Page, creds: dict) -> None:
    log.info("Logging in as '%s'", creds["id"])
    page.goto(LOGIN_URL)
    page.wait_for_load_state("domcontentloaded")
    page.fill('input[name="loginId"]', creds["id"])
    page.fill('input[name="passwd"]', creds["password"])
    # a.btn_yes は javascript:goSubmit(...) を呼ぶため、フォームsubmitによる
    # ナビゲーションを expect_navigation で明示的に待機する
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
    url = BP_URL.format(date=record["date"])
    page.goto(url)
    page.wait_for_load_state("domcontentloaded")

    # Ensure the input form is visible (existing data may hide it via JS)
    page.evaluate(
        "var el = document.getElementById('input_form'); if (el) el.style.display = 'block';"
    )

    # Fill time
    page.locator('input[name="measurementTimeHH"]').fill(record["hh"])
    page.locator('input[name="measurementTimeMM"]').fill(record["mm"])

    # Fill blood pressure and pulse
    page.locator('input[name="bloodpressureBean[0].keyData"]').fill(str(record["sys"]))
    page.locator('input[name="bloodpressureBean[1].keyData"]').fill(str(record["dia"]))
    page.locator('input[name="bloodpressureBean[2].keyData"]').fill(
        str(record["pulse"])
    )

    if dry_run:
        log.info(
            "[DRY RUN] %s %s:%s — sys=%s dia=%s pulse=%s",
            record["date"],
            record["hh"],
            record["mm"],
            record["sys"],
            record["dia"],
            record["pulse"],
        )
        return True

    # 登録ボタン (goSubmit → forms[0].submit → bloodpressure_confirm.do へ遷移)
    with page.expect_navigation():
        page.evaluate(
            "document.forms[0].method='POST';"
            "document.forms[0].action='/bloodpressure_confirm.do';"
            "document.forms[0].submit();"
        )
    page.wait_for_load_state("domcontentloaded")

    # 確認ページの「登録」ボタンをクリック
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
        description="Upload bptracker data to Health Planet"
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
    records = load_bp_records()

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
                    "[%d/%d] %s %s:%s sys=%s dia=%s pulse=%s",
                    i,
                    len(records),
                    record["date"],
                    record["hh"],
                    record["mm"],
                    record["sys"],
                    record["dia"],
                    record["pulse"],
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
