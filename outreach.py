#!/usr/bin/env python3
"""Safe, CSV-driven WhatsApp outreach MVP using desktop browser automation.

Dry-run is the default. Real browser automation requires --send and an
interactive confirmation (or --yes for an explicitly unattended invocation).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import string
import sys
import time
import unicodedata
import webbrowser
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

import phonenumbers
import pycountry

TRUTHY = {"1", "true", "yes", "y"}
REQUIRED_COLUMNS = {"first_name", "phone", "programme", "whatsapp_consent", "opt_out"}
PREPARE_REQUIRED_COLUMNS = REQUIRED_COLUMNS | {"country"}
LOG_COLUMNS = [
    "timestamp_utc", "campaign", "row_number", "first_name", "phone",
    "status", "template_id", "delay_seconds", "message", "error",
]
REVIEW_COLUMNS = ["campaign", "row_number", "first_name", "phone", "programme", "reason"]
PREPARED_COLUMNS = [
    "first_name", "phone", "country", "programme", "whatsapp_consent",
    "opt_out", "original_phone", "validation_status", "detected_region",
]
PREPARATION_REVIEW_COLUMNS = PREPARED_COLUMNS + ["validation_reason"]
ALLOWED_TEMPLATE_FIELDS = {"first_name", "programme", "academy_name"}
OPT_OUT_TEXT = "Reply STOP if you do not want further WhatsApp messages."
COUNTRY_ALIASES = {
    # Primary admissions markets
    "colombia": "CO",
    "costa rica": "CR",
    "mexico": "MX",
    "chile": "CL",
    # Other Spanish-speaking Latin American countries and territories
    "argentina": "AR",
    "bolivia": "BO",
    "cuba": "CU",
    "republica dominicana": "DO",
    "dominicana": "DO",
    "ecuador": "EC",
    "el salvador": "SV",
    "salvador": "SV",
    "guatemala": "GT",
    "honduras": "HN",
    "nicaragua": "NI",
    "panama": "PA",
    "paraguay": "PY",
    "peru": "PE",
    "puerto rico": "PR",
    "uruguay": "UY",
    "venezuela": "VE",
    "svg": "VC",
    "saint vincent": "VC",
    "st vincent": "VC",
    "st vincent and the grenadines": "VC",
    "trinidad": "TT",
    "tobago": "TT",
    "trinidad and tobago": "TT",
    "t&t": "TT",
    "barbados": "BB",
    "grenada": "GD",
    "saint lucia": "LC",
    "st lucia": "LC",
}


@dataclass(frozen=True)
class Prospect:
    row_number: int
    first_name: str
    phone: str
    programme: str
    whatsapp_consent: bool
    opted_out: bool


class MessageBank:
    """Validated shuffle-bag message selection with balanced random rotation."""

    def __init__(
        self, messages: list[dict[str, str]], opt_out_text: str = OPT_OUT_TEXT
    ):
        self.messages = messages
        self.opt_out_text = opt_out_text.strip()
        self._queue: list[dict[str, str]] = []
        self._random = random.SystemRandom()

    def next(self, prospect: Prospect, academy_name: str) -> tuple[str, str]:
        if not self._queue:
            self._queue = self.messages.copy()
            self._random.shuffle(self._queue)
        template = self._queue.pop()
        message = template["text"].format(
            first_name=prospect.first_name,
            programme=prospect.programme,
            academy_name=academy_name,
        ).strip()
        rendered = f"{message} {self.opt_out_text}" if self.opt_out_text else message
        return template["id"], rendered


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


def normalize_country_key(value: str) -> str:
    ascii_value = "".join(
        character
        for character in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(character)
    )
    return re.sub(
        r"\s+", " ", re.sub(r"[^a-z0-9&]+", " ", ascii_value.lower())
    ).strip()


def resolve_country_region(value: str) -> str | None:
    key = normalize_country_key(value)
    if not key:
        return None
    if key in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[key]
    try:
        return pycountry.countries.lookup(value.strip()).alpha_2
    except LookupError:
        return None


def normalize_phone_from_country(
    value: str, country: str
) -> tuple[str | None, str, str, str]:
    """Return E.164, status, reason, and detected ISO region."""
    raw = value.strip()
    if not raw:
        return None, "invalid_number", "phone is empty", ""

    region = resolve_country_region(country)
    international = raw.startswith("+") or raw.startswith("00")
    if not international and region is None:
        reason = "country is missing" if not country.strip() else "country is not recognized"
        status = "country_missing" if not country.strip() else "human_review"
        return None, status, reason, ""

    parse_value = f"+{raw[2:]}" if raw.startswith("00") else raw
    try:
        parsed = phonenumbers.parse(parse_value, None if international else region)
    except phonenumbers.NumberParseException as exc:
        return None, "invalid_number", str(exc), ""

    if not phonenumbers.is_possible_number(parsed):
        return None, "invalid_number", "number has an impossible length or structure", ""
    if not phonenumbers.is_valid_number(parsed):
        return None, "invalid_number", "number is not valid for its numbering plan", ""

    detected = phonenumbers.region_code_for_number(parsed) or ""
    if region and detected and region != detected:
        return None, "country_mismatch", f"country indicates {region}, number indicates {detected}", detected

    normalized = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    status = "valid_original" if international else "corrected_from_country"
    return normalized, status, "", detected


def generate_message(prospect: Prospect, academy_name: str) -> str:
    return (
        f"Hello {prospect.first_name}, this is {academy_name}. "
        f"You expressed interest in {prospect.programme}. "
        "Would you like information about the admission requirements and application process? "
        "Reply STOP if you do not want further WhatsApp messages."
    )


def load_message_bank(path: Path) -> MessageBank:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    messages = payload.get("messages") if isinstance(payload, dict) else payload
    opt_out_text = (
        str(payload.get("opt_out_text", OPT_OUT_TEXT))
        if isinstance(payload, dict)
        else OPT_OUT_TEXT
    )
    if not isinstance(messages, list) or len(messages) < 10:
        raise ValueError("The message bank must contain at least 10 messages.")

    validated: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    formatter = string.Formatter()
    for index, item in enumerate(messages, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Message {index} must be a JSON object.")
        template_id = str(item.get("id", "")).strip()
        text = str(item.get("text", "")).strip()
        if not template_id or not text:
            raise ValueError(f"Message {index} requires non-empty id and text values.")
        if template_id in seen_ids:
            raise ValueError(f"Duplicate message id: {template_id}")
        fields = {field for _, field, _, _ in formatter.parse(text) if field}
        unsupported = fields - ALLOWED_TEMPLATE_FIELDS
        if unsupported:
            raise ValueError(
                f"Message {template_id} has unsupported placeholders: "
                f"{', '.join(sorted(unsupported))}"
            )
        seen_ids.add(template_id)
        validated.append({"id": template_id, "text": text})
    return MessageBank(validated, opt_out_text)


def read_csv(
    path: Path, required_columns: set[str] = REQUIRED_COLUMNS
) -> tuple[list[dict[str, str]], list[str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = sorted(required_columns - set(reader.fieldnames or []))
        return ([], missing) if missing else (list(reader), [])


def write_csv(path: Path, columns: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def prepare_dataset(
    input_path: Path,
    cleaned_path: Path,
    review_path: Path,
    do_not_contact_path: Path,
    default_region: str,
) -> dict[str, int]:
    rows, missing = read_csv(input_path, PREPARE_REQUIRED_COLUMNS)
    if missing:
        raise ValueError(f"Missing required CSV columns: {', '.join(missing)}")

    blocked = load_do_not_contact(do_not_contact_path, default_region)
    cleaned: list[dict[str, object]] = []
    review: list[dict[str, object]] = []
    seen: set[str] = set()
    counts: dict[str, int] = {}

    for row in rows:
        original_phone = (row.get("phone") or "").strip()
        country = (row.get("country") or "").strip()
        normalized, status, reason, detected = normalize_phone_from_country(
            original_phone, country
        )
        consent = is_truthy(row.get("whatsapp_consent"))
        opted_out = is_truthy(row.get("opt_out"))

        if normalized and normalized in seen:
            status, reason = "duplicate", "normalized phone already appears in this dataset"
        elif normalized:
            seen.add(normalized)
        if normalized and normalized in blocked:
            status, reason = "do_not_contact", "number appears in the do-not-contact list"
        elif opted_out:
            status, reason = "opted_out", "contact has opted out"
        elif not consent:
            status, reason = "no_consent", "WhatsApp consent is not affirmative"
        elif not (row.get("first_name") or "").strip() or not (row.get("programme") or "").strip():
            status, reason = "human_review", "first_name or programme is missing"

        output = {
            "first_name": (row.get("first_name") or "").strip(),
            "phone": normalized or "",
            "country": country,
            "programme": (row.get("programme") or "").strip(),
            "whatsapp_consent": "yes" if consent else "no",
            "opt_out": "yes" if opted_out else "no",
            "original_phone": original_phone,
            "validation_status": status,
            "detected_region": detected,
            "validation_reason": reason,
        }
        counts[status] = counts.get(status, 0) + 1
        if status in {"valid_original", "corrected_from_country"}:
            cleaned.append(output)
        else:
            review.append(output)

    write_csv(cleaned_path, PREPARED_COLUMNS, cleaned)
    write_csv(review_path, PREPARATION_REVIEW_COLUMNS, review)
    return counts


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
    if exists:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            existing_rows = list(reader)
            existing_columns = list(reader.fieldnames or [])
        if existing_columns and existing_columns != columns:
            temporary = path.with_suffix(path.suffix + ".tmp")
            write_csv(temporary, columns, existing_rows)
            os.replace(temporary, path)
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def log_result(
    path: Path,
    campaign: str,
    prospect: Prospect,
    status: str,
    message: str = "",
    error: str = "",
    template_id: str = "",
    delay_seconds: int | str = "",
) -> None:
    append_row(path, LOG_COLUMNS, {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "campaign": campaign,
        "row_number": prospect.row_number,
        "first_name": prospect.first_name,
        "phone": prospect.phone,
        "status": status,
        "template_id": template_id,
        "delay_seconds": delay_seconds,
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
    """Open, focus, and submit a pre-filled WhatsApp Web message on Windows.

    PyWhatKit's instant sender can lose browser focus before its final Enter
    keypress. This small adapter uses the same browser-automation approach but
    explicitly finds the WhatsApp browser window before submitting.
    """
    import pyautogui
    import pygetwindow

    recipient = phone.lstrip("+")
    url = (
        f"https://web.whatsapp.com/send?phone={quote(recipient)}"
        f"&text={quote(message)}"
    )
    if not webbrowser.open(url, new=2):
        raise RuntimeError("The default browser could not be opened.")

    time.sleep(wait_time)
    candidates = [
        window
        for window in pygetwindow.getAllWindows()
        if "whatsapp" in (window.title or "").lower()
    ]
    if not candidates:
        raise RuntimeError(
            "WhatsApp Web opened, but its browser window could not be found. "
            "Keep the browser visible and make it your default browser."
        )

    browser_words = ("chrome", "edge", "firefox", "brave", "opera")
    window = next(
        (
            item
            for item in candidates
            if any(word in item.title.lower() for word in browser_words)
        ),
        candidates[0],
    )
    if window.isMinimized:
        window.restore()
    window.activate()
    time.sleep(2)

    # WhatsApp's composer is near the bottom-centre of the active chat pane.
    click_x = window.left + (window.width // 2)
    click_y = window.top + window.height - 65
    pyautogui.click(click_x, click_y)
    time.sleep(1)
    pyautogui.press("enter")
    time.sleep(max(close_time, 2))
    pyautogui.hotkey("ctrl", "w")


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prepare",
        action="store_true",
        help="Clean the input dataset and exit without sending",
    )
    parser.add_argument("--input", type=Path, default=Path("prospects.csv"))
    parser.add_argument("--cleaned-output", type=Path, default=Path("cleaned_prospects.csv"))
    parser.add_argument("--campaign", help="Unique campaign name")
    parser.add_argument("--academy-name")
    parser.add_argument("--messages", type=Path, help="JSON bank containing at least 10 messages")
    parser.add_argument("--log", type=Path, default=Path("outreach_log.csv"))
    parser.add_argument("--review", type=Path, default=Path("review_queue.csv"))
    parser.add_argument("--do-not-contact", type=Path, default=Path("do_not_contact.csv"))
    parser.add_argument("--default-region", default="VC")
    parser.add_argument("--max-messages", type=int, default=10)
    parser.add_argument("--delay", type=int, help="Use a fixed delay instead of random pacing")
    parser.add_argument("--min-delay", type=int, default=45)
    parser.add_argument("--max-delay", type=int, default=90)
    parser.add_argument("--wait-time", type=int, default=20)
    parser.add_argument("--close-time", type=int, default=3)
    parser.add_argument("--confirm-each", action="store_true")
    parser.add_argument("--send", action="store_true", help="Enable real WhatsApp Web automation")
    parser.add_argument("--yes", action="store_true", help="Skip the one-time SEND confirmation")
    args = parser.parse_args(argv)
    if not args.prepare and (not args.campaign or not args.academy_name):
        parser.error("--campaign and --academy-name are required unless --prepare is used")
    if args.max_messages < 1:
        parser.error("--max-messages must be at least 1")
    if args.delay is not None and args.delay < 0:
        parser.error("--delay cannot be negative")
    if args.min_delay < 0 or args.max_delay < args.min_delay:
        parser.error("--min-delay must be non-negative and not exceed --max-delay")
    if args.wait_time < 1 or args.close_time < 0:
        parser.error("delay/close-time cannot be negative and wait-time must be positive")
    return args


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.input.exists():
        print(f"Input file not found: {args.input}", file=sys.stderr)
        return 2
    if args.prepare:
        try:
            counts = prepare_dataset(
                args.input,
                args.cleaned_output,
                args.review,
                args.do_not_contact,
                args.default_region,
            )
        except (ValueError, OSError) as exc:
            print(f"Preparation failed: {exc}", file=sys.stderr)
            return 2
        print("Dataset preparation completed; no messages were sent.")
        print(f"Cleaned: {args.cleaned_output}")
        print(f"Review: {args.review}")
        for status, count in sorted(counts.items()):
            print(f"  {status}: {count}")
        return 0

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
    try:
        message_bank = load_message_bank(args.messages) if args.messages else None
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"Invalid message bank: {exc}", file=sys.stderr)
        return 2
    delay_random = random.SystemRandom()
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

        if message_bank:
            template_id, message = message_bank.next(prospect, args.academy_name)
        else:
            template_id, message = "default", generate_message(prospect, args.academy_name)
        planned_delay = (
            args.delay
            if args.delay is not None
            else delay_random.randint(args.min_delay, args.max_delay)
        )
        print(f"\n{prospect.first_name} ({phone}) [template: {template_id}]\n{message}")
        if args.confirm_each and input("Process this message? [y/N]: ").strip().lower() != "y":
            log_result(
                args.log, args.campaign, prospect, "skipped_by_operator",
                message, template_id=template_id,
            )
            continue
        if not args.send:
            log_result(
                args.log, args.campaign, prospect, "dry_run", message,
                template_id=template_id, delay_seconds=planned_delay,
            )
            print(f"Planned delay before the next send: {planned_delay} seconds")
            continue
        try:
            send_whatsapp(phone, message, args.wait_time, args.close_time)
            sent_count += 1
            # Browser automation can confirm only that sending was requested.
            log_result(
                args.log, args.campaign, prospect, "send_requested", message,
                template_id=template_id, delay_seconds=planned_delay,
            )
        except Exception as exc:
            log_result(
                args.log, args.campaign, prospect, "failed", message, str(exc),
                template_id=template_id,
            )
        if sent_count < args.max_messages:
            print(f"Waiting {planned_delay} seconds before the next send...")
            time.sleep(planned_delay)

    print(f"\nFinished. Real send requests this run: {sent_count}. Log: {args.log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
