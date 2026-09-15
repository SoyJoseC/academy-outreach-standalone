#!/usr/bin/env python3
"""Safe, CSV-driven WhatsApp outreach MVP using PyWhatKit.

Dry-run is the default. Real browser automation requires --send and an
interactive confirmation (or --yes for an explicitly unattended invocation).
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import phonenumbers

TRUTHY = {"1", "true", "yes", "y"}
REQUIRED_COLUMNS = {"first_name", "phone", "programme", "whatsapp_consent", "opt_out"}
LOG_COLUMNS = ["timestamp_utc", "campaign", "row_number", "first_name", "phone", "status", "message", "error"]
REVIEW_COLUMNS = ["campaign", "row_number", "first_name", "phone", "programme", "reason"]


@dataclass(frozen=True)
class Prospect:
    row_number: int
    first_name: str
    phone: str
    programme: str
    whatsapp_consent: bool
    opted_out: bool


def is_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in TRUTHY


def normalize_phone(value: str, default_region: str) -> str | None:
    try:
        parsed = phonenumbers.parse(value.strip(), default_region)
    except (phonenumbers.NumberParseException, AttributeError):
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def generate_message(prospect: Prospect, academy_name: str) -> str:
    return (
        f"Hello {prospect.first_name}, this is {academy_name}. "
        f"You expressed interest in {prospect.programme}. "
        "Would you like information about the admission requirements and application process? "
        "Reply STOP if you do not want further WhatsApp messages."
    )


def read_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = sorted(REQUIRED_COLUMNS - set(reader.fieldnames or []))
        return ([], missing) if missing else (list(reader), [])


def load_do_not_contact(path: Path | None, default_region: str) -> set[str]:
    if path is None or not path.exists():
        return set()
    blocked: set[str] = set()
    with path.open(encoding="utf-8-sig") as handle:
        for raw in handle:
            value = raw.strip()
            if not value or value.lower() == "phone":
                continue
            normalized = normalize_phone(value, default_region)
            if normalized:
                blocked.add(normalized)
    return blocked


def load_previous_sends(path: Path, campaign: str) -> set[str]:
    if not path.exists():
        return set()
    with path.open(newline="", encoding="utf-8") as handle:
        return {
            row.get("phone", "")
            for row in csv.DictReader(handle)
            if row.get("campaign") == campaign and row.get("status") == "send_requested"
        }


def append_row(path: Path, columns: list[str], row: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def log_result(path: Path, campaign: str, prospect: Prospect, status: str, message: str = "", error: str = "") -> None:
    append_row(path, LOG_COLUMNS, {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "campaign": campaign,
        "row_number": prospect.row_number,
        "first_name": prospect.first_name,
        "phone": prospect.phone,
        "status": status,
        "message": message,
        "error": error,
    })


def add_to_review(path: Path, campaign: str, prospect: Prospect, reason: str) -> None:
    append_row(path, REVIEW_COLUMNS, {
        "campaign": campaign,
        "row_number": prospect.row_number,
        "first_name": prospect.first_name,
        "phone": prospect.phone,
        "programme": prospect.programme,
        "reason": reason,
    })


def send_whatsapp(phone: str, message: str, wait_time: int, close_time: int) -> None:
    import pywhatkit  # Imported only when real sending is explicitly enabled.

    pywhatkit.sendwhatmsg_instantly(
        phone, message, wait_time=wait_time, tab_close=True, close_time=close_time
    )


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("prospects.csv"))
    parser.add_argument("--campaign", required=True, help="Unique campaign name")
    parser.add_argument("--academy-name", required=True)
    parser.add_argument("--log", type=Path, default=Path("outreach_log.csv"))
    parser.add_argument("--review", type=Path, default=Path("review_queue.csv"))
    parser.add_argument("--do-not-contact", type=Path, default=Path("do_not_contact.csv"))
    parser.add_argument("--default-region", default="VC")
    parser.add_argument("--max-messages", type=int, default=10)
    parser.add_argument("--delay", type=int, default=60)
    parser.add_argument("--wait-time", type=int, default=20)
    parser.add_argument("--close-time", type=int, default=3)
    parser.add_argument("--confirm-each", action="store_true")
    parser.add_argument("--send", action="store_true", help="Enable real WhatsApp Web automation")
    parser.add_argument("--yes", action="store_true", help="Skip the one-time SEND confirmation")
    args = parser.parse_args(argv)
    if args.max_messages < 1:
        parser.error("--max-messages must be at least 1")
    if args.delay < 0 or args.wait_time < 1 or args.close_time < 0:
        parser.error("delay/close-time cannot be negative and wait-time must be positive")
    return args


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.input.exists():
        print(f"Input file not found: {args.input}", file=sys.stderr)
        return 2
    rows, missing = read_csv(args.input)
    if missing:
        print(f"Missing required CSV columns: {', '.join(missing)}", file=sys.stderr)
        return 2
    if args.send and not args.yes:
        confirmation = input(f"REAL SEND is enabled (limit {args.max_messages}). Type SEND to continue: ")
        if confirmation != "SEND":
            print("Cancelled; no messages were sent.")
            return 1

    print(f"Academy Outreach — {'REAL SEND' if args.send else 'DRY RUN'} — campaign: {args.campaign}")
    blocked = load_do_not_contact(args.do_not_contact, args.default_region)
    previously_sent = load_previous_sends(args.log, args.campaign)
    seen: set[str] = set()
    sent_count = 0

    for row_number, row in enumerate(rows, start=2):
        raw_phone = (row.get("phone") or "").strip()
        phone = normalize_phone(raw_phone, args.default_region) or ""
        prospect = Prospect(
            row_number=row_number,
            first_name=(row.get("first_name") or "").strip(),
            phone=phone or raw_phone,
            programme=(row.get("programme") or "").strip(),
            whatsapp_consent=is_truthy(row.get("whatsapp_consent")),
            opted_out=is_truthy(row.get("opt_out")),
        )
        if prospect.opted_out:
            log_result(args.log, args.campaign, prospect, "skipped_opt_out")
            continue
        if not prospect.whatsapp_consent:
            log_result(args.log, args.campaign, prospect, "skipped_no_consent")
            continue
        if not phone:
            log_result(args.log, args.campaign, prospect, "invalid_phone")
            continue
        prospect = replace(prospect, phone=phone)
        if phone in blocked:
            log_result(args.log, args.campaign, prospect, "skipped_do_not_contact")
            continue
        if phone in seen:
            log_result(args.log, args.campaign, prospect, "skipped_duplicate_input")
            continue
        seen.add(phone)
        if phone in previously_sent:
            log_result(args.log, args.campaign, prospect, "skipped_already_requested")
            continue
        if not prospect.first_name or not prospect.programme:
            reason = "missing first_name or programme"
            add_to_review(args.review, args.campaign, prospect, reason)
            log_result(args.log, args.campaign, prospect, "human_review", error=reason)
            continue
        if sent_count >= args.max_messages:
            log_result(args.log, args.campaign, prospect, "skipped_session_limit")
            continue

        message = generate_message(prospect, args.academy_name)
        print(f"\n{prospect.first_name} ({phone})\n{message}")
        if args.confirm_each and input("Process this message? [y/N]: ").strip().lower() != "y":
            log_result(args.log, args.campaign, prospect, "skipped_by_operator", message)
            continue
        if not args.send:
            log_result(args.log, args.campaign, prospect, "dry_run", message)
            continue
        try:
            send_whatsapp(phone, message, args.wait_time, args.close_time)
            sent_count += 1
            # Browser automation can confirm only that sending was requested.
            log_result(args.log, args.campaign, prospect, "send_requested", message)
        except Exception as exc:
            log_result(args.log, args.campaign, prospect, "failed", message, str(exc))
        if sent_count < args.max_messages:
            time.sleep(args.delay)

    print(f"\nFinished. Real send requests this run: {sent_count}. Log: {args.log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
