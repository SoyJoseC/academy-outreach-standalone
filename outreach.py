#!/usr/bin/env python3
"""Safe, CSV-driven WhatsApp outreach MVP using desktop browser automation.

Dry-run is the default. Real browser automation requires --send and an
interactive confirmation (or --yes for an explicitly unattended invocation).

--country limits a run to Colombia, Mexico, Costa Rica, Chile, or everyone
else. Each of those groups can use its own message bank.
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
from urllib.request import Request, urlopen

import phonenumbers
import pycountry

TRUTHY = {"1", "true", "yes", "y"}
REQUIRED_COLUMNS = {"first_name", "phone"}
PREPARE_REQUIRED_COLUMNS = REQUIRED_COLUMNS | {"country"}
LOG_COLUMNS = [
    "timestamp_utc", "campaign", "row_number", "first_name", "phone", "country",
    "status", "template_id", "delay_seconds", "message", "error",
]
REVIEW_COLUMNS = ["campaign", "row_number", "first_name", "phone", "programme", "reason"]
PREPARED_COLUMNS = [
    "first_name", "last_name", "phone", "country", "sign_up_comment",
    "date created", "opt_out", "original_phone", "validation_status", "detected_region",
]
CONTACT_ORDER_OLDEST_FIRST = "oldest-first"
CONTACT_ORDER_FILE = "file-order"
PRIMARY_REGIONS = frozenset({"CO", "MX", "CR", "CL"})
COUNTRY_FILTER_ALIASES = {
    "colombia": "CO",
    "co": "CO",
    "mexico": "MX",
    "mx": "MX",
    "costa rica": "CR",  # --country costa_rica (underscores normalize to spaces)
    "costarica": "CR",
    "cr": "CR",
    "chile": "CL",
    "cl": "CL",
    "other": "other",
    "others": "other",
    "otro": "other",
    "otros": "other",
}
COUNTRY_GROUP_LABELS = {
    "CO": "Colombia",
    "MX": "Mexico",
    "CR": "Costa Rica",
    "CL": "Chile",
    "other": "other",
}
COUNTRY_GROUP_ORDER = ("CO", "MX", "CR", "CL", "other")
STRIP_EXPORT_HEADERS = [
    "first name", "last name", "phone", "country", "sign up comment", "date created",
]
STRIP_EXPORT_FIELDS = [
    "first_name", "last_name", "phone", "country", "sign_up_comment", "date_created",
]
PREPARATION_REVIEW_COLUMNS = PREPARED_COLUMNS + ["validation_reason"]
ALLOWED_TEMPLATE_FIELDS = {
    "first_name", "last_name", "programme", "sign_up_comment", "academy_name",
    "country",
}
COLUMN_ALIASES = {
    "first name": "first_name",
    "firstname": "first_name",
    "last name": "last_name",
    "lastname": "last_name",
    "phone number": "phone",
    "mobile": "phone",
    "mobile number": "phone",
    "program": "programme",
    "course": "programme",
    "sign up comment": "sign_up_comment",
    "signup comment": "sign_up_comment",
    "comments": "sign_up_comment",
    "date created": "date_created",
    "datecreated": "date_created",
    "date_created": "date_created",
    "created at": "date_created",
    "meta country": "meta_country",
    "country of residence": "country",
    "whatsapp consent": "whatsapp_consent",
    "opt out": "opt_out",
}
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
    last_name: str = ""
    sign_up_comment: str = ""
    date_created: str = ""
    country: str = ""


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
            last_name=prospect.last_name,
            programme=prospect.programme,
            sign_up_comment=prospect.sign_up_comment,
            academy_name=academy_name,
            country=prospect.country,
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


def iter_country_message_paths(args: argparse.Namespace):
    """Message banks for the four main markets. Keys match PRIMARY_REGIONS."""
    yield "CO", args.messages_colombia
    yield "MX", args.messages_mexico
    yield "CR", args.messages_costa_rica
    yield "CL", args.messages_chile


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
    interest = (
        f" You expressed interest in {prospect.programme}."
        if prospect.programme
        else ""
    )
    return (
        f"Hello {prospect.first_name}, this is {academy_name}.{interest} "
        "Would you like information about the admission requirements and application process? "
        f"{OPT_OUT_TEXT}"
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


def normalize_column_name(column: str) -> str:
    spaced = normalize_country_key(column or "").replace("_", " ")
    if spaced in COLUMN_ALIASES:
        return COLUMN_ALIASES[spaced]
    snake = normalize_country_key(column or "").replace(" ", "_")
    return COLUMN_ALIASES.get(spaced, snake)


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return []
        normalized_fields = [normalize_column_name(column) for column in reader.fieldnames]
        rows: list[dict[str, str]] = []
        for raw_row in reader:
            merged: dict[str, str] = {}
            for field, value in zip(normalized_fields, raw_row.values()):
                text = "" if value is None else str(value)
                if field not in merged or not merged[field].strip():
                    merged[field] = text
            rows.append(merged)
        return rows


def read_csv(
    path: Path, required_columns: set[str] = REQUIRED_COLUMNS
) -> tuple[list[dict[str, str]], list[str]]:
    rows = read_csv_rows(path)
    if rows:
        present = set(rows[0].keys())
    else:
        present = set()
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            present = {normalize_column_name(column) for column in (reader.fieldnames or [])}
    if "country" in required_columns and "country" not in present and "meta_country" in present:
        present = set(present) | {"country"}
    missing = sorted(required_columns - present)
    return ([], missing) if missing else (rows, [])


def country_from_row(row: dict[str, str]) -> str:
    country = (row.get("country") or "").strip()
    if country:
        return country
    return (row.get("meta_country") or "").strip()


def default_strip_output_path(input_path: Path) -> Path:
    stem = input_path.stem
    if stem.endswith("_cleaned"):
        return input_path.with_name(f"{stem}_columns{input_path.suffix}")
    return input_path.with_name(f"{stem}_cleaned{input_path.suffix}")


def default_prepare_cleaned_path(input_path: Path) -> Path:
    return input_path.with_name(f"{input_path.stem}_prepared{input_path.suffix}")


def strip_export_columns(input_path: Path, output_path: Path) -> int:
    """Keep six outreach columns for every row; preserve order and empty cells."""
    rows = read_csv_rows(input_path)
    output_rows: list[dict[str, str]] = []
    for row in rows:
        export_row = {
            "first_name": (row.get("first_name") or "").strip(),
            "last_name": (row.get("last_name") or "").strip(),
            "phone": (row.get("phone") or "").strip(),
            "country": country_from_row(row),
            "sign_up_comment": (row.get("sign_up_comment") or "").strip(),
            "date_created": (row.get("date_created") or "").strip(),
        }
        output_rows.append(
            {
                header: export_row[field]
                for header, field in zip(STRIP_EXPORT_HEADERS, STRIP_EXPORT_FIELDS)
            }
        )
    write_csv(output_path, STRIP_EXPORT_HEADERS, output_rows)
    return len(output_rows)


def parse_date_created(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def row_date_created_raw(row: dict[str, str]) -> str:
    return (row.get("date_created") or row.get("date created") or "").strip()


def as_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def parse_country_selection(values: list[str]) -> set[str]:
    """Map CLI country names to Colombia, Mexico, Costa Rica, Chile, or other."""
    selected: set[str] = set()
    unknown: list[str] = []
    for raw in values:
        for piece in raw.split(","):
            key = normalize_country_key(piece)
            if not key:
                continue
            group = COUNTRY_FILTER_ALIASES.get(key)
            if group is None:
                unknown.append(piece.strip())
            else:
                selected.add(group)
    if unknown:
        names = ", ".join(unknown)
        raise ValueError(
            f"Unknown country filter: {names}. "
            "Use colombia, mexico, costa_rica, chile, or other."
        )
    return selected


def parse_filter_bound(value: str, *, end_of_day: bool) -> datetime:
    text = value.strip()
    if not text:
        raise ValueError("A date filter cannot be empty. Use YYYY-MM-DD.")
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt == "%Y-%m-%d" and end_of_day:
            return parsed.replace(hour=23, minute=59, second=59)
        return parsed
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Could not parse date '{value}'. Use YYYY-MM-DD.") from exc
    parsed = as_naive_utc(parsed)
    if end_of_day and "T" not in text and " " not in text:
        return parsed.replace(hour=23, minute=59, second=59)
    return parsed


def region_from_phone(value: str) -> str | None:
    raw = (value or "").strip()
    if not (raw.startswith("+") or raw.startswith("00")):
        return None
    parse_value = f"+{raw[2:]}" if raw.startswith("00") else raw
    try:
        parsed = phonenumbers.parse(parse_value, None)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.region_code_for_number(parsed) or None


def contact_region(row: dict[str, str]) -> str | None:
    region = resolve_country_region(country_from_row(row))
    if region:
        return region
    return region_from_phone(row.get("phone") or "")


def row_matches_country(row: dict[str, str], groups: set[str]) -> bool:
    if not groups:
        return True
    region = contact_region(row)
    if region in groups:
        return True
    return "other" in groups and region not in PRIMARY_REGIONS


def row_matches_date(
    row: dict[str, str], start: datetime | None, end: datetime | None
) -> bool:
    if start is None and end is None:
        return True
    parsed = parse_date_created(row_date_created_raw(row))
    if parsed is None:
        return False
    parsed = as_naive_utc(parsed)
    if start is not None and parsed < start:
        return False
    if end is not None and parsed > end:
        return False
    return True


def describe_contact_filter(
    country_groups: set[str], date_from: str, date_to: str, on_date: str
) -> str:
    parts: list[str] = []
    if country_groups:
        labels = [
            COUNTRY_GROUP_LABELS[group]
            for group in COUNTRY_GROUP_ORDER
            if group in country_groups
        ]
        parts.append("country: " + ", ".join(labels))
    if on_date:
        parts.append(f"date created: {on_date}")
    elif date_from or date_to:
        start = date_from or "…"
        finish = date_to or "…"
        parts.append(f"date created: {start} to {finish}")
    return "; ".join(parts)


def sort_rows_by_date_created(
    rows: list[dict[str, str]], *, oldest_first: bool = True
) -> list[dict[str, str]]:
    """Put oldest inquiries first; rows without a parseable date go last."""
    if not oldest_first or not rows:
        return rows

    def sort_key(row: dict[str, str]) -> tuple[int, datetime]:
        parsed = parse_date_created(row_date_created_raw(row))
        if parsed is None:
            return (1, datetime.max)
        return (0, parsed)

    return sorted(rows, key=sort_key)


def rows_have_date_created(rows: list[dict[str, str]]) -> bool:
    return any(parse_date_created(row_date_created_raw(row)) for row in rows)


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
        country = country_from_row(row)
        normalized, status, reason, detected = normalize_phone_from_country(
            original_phone, country
        )
        consent_supplied = "whatsapp_consent" in row
        consent = is_truthy(row.get("whatsapp_consent")) if consent_supplied else True
        opted_out = is_truthy(row.get("opt_out"))

        if normalized and normalized in seen:
            status, reason = "duplicate", "normalized phone already appears in this dataset"
        elif normalized:
            seen.add(normalized)
        if normalized and normalized in blocked:
            status, reason = "do_not_contact", "number appears in the do-not-contact list"
        elif opted_out:
            status, reason = "opted_out", "contact has opted out"
        elif consent_supplied and not consent:
            status, reason = "no_consent", "WhatsApp consent is not affirmative"
        elif not (row.get("first_name") or "").strip():
            status, reason = "human_review", "first_name is missing"

        output = {
            "first_name": (row.get("first_name") or "").strip(),
            "last_name": (row.get("last_name") or "").strip(),
            "phone": normalized or "",
            "country": country,
            "sign_up_comment": (row.get("sign_up_comment") or "").strip(),
            "date created": (row.get("date_created") or "").strip(),
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

    cleaned = sort_rows_by_date_created(cleaned, oldest_first=True)
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


def eligible_phone_numbers(
    rows: list[dict[str, str]], blocked: set[str], default_region: str
) -> set[str]:
    """Return unique contacts that are eligible for campaign progress."""
    eligible: set[str] = set()
    for row in rows:
        phone = normalize_phone((row.get("phone") or "").strip(), default_region)
        consent_denied = (
            "whatsapp_consent" in row and not is_truthy(row.get("whatsapp_consent"))
        )
        if (
            phone
            and phone not in blocked
            and not is_truthy(row.get("opt_out"))
            and not consent_denied
            and (row.get("first_name") or "").strip()
        ):
            eligible.add(phone)
    return eligible


def campaign_progress(
    eligible: set[str], previously_sent: set[str], sent_this_run: int = 0
) -> tuple[int, int, int, float]:
    """Return reached, total, remaining, and percentage for one campaign."""
    total = len(eligible)
    reached = min(len(eligible & previously_sent) + sent_this_run, total)
    remaining = total - reached
    percentage = (reached / total * 100) if total else 0.0
    return reached, total, remaining, percentage


def print_campaign_progress(
    eligible: set[str], previously_sent: set[str], sent_this_run: int = 0
) -> None:
    reached, total, remaining, percentage = campaign_progress(
        eligible, previously_sent, sent_this_run
    )
    print(
        f"Campaign progress: {reached}/{total} reached "
        f"({percentage:.1f}%) — {remaining} remaining"
    )


def print_run_progress(sent_this_run: int, run_target: int) -> None:
    print(f"Run progress: {sent_this_run}/{run_target} contacted this run")


def send_ntfy_notification(
    server: str,
    topic: str,
    campaign: str,
    sent_this_run: int,
    run_target: int,
    reached: int,
    total: int,
    remaining: int,
    token: str = "",
) -> None:
    """Publish a batch-completion notification to ntfy."""
    payload = {
        "topic": topic,
        "title": "Academy Outreach batch finished",
        "message": (
            f"Campaign {campaign}: {sent_this_run}/{run_target} contacted this run. "
            f"Overall progress: {reached}/{total} reached; {remaining} remaining."
        ),
        "tags": ["white_check_mark"],
    }
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        server.rstrip("/"),
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"ntfy returned HTTP {response.status}")


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
        "country": prospect.country,
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


BROWSER_WORDS = (
    "chrome", "chromium", "edge", "firefox", "brave", "opera", "vivaldi",
    "librewolf", "floorp", "zen", "falkon",
)
WINDOW_SEARCH_SECONDS = 20
FOCUS_ATTEMPTS = 4
ENTER_PRESS_GAP_SECONDS = 1.5


@dataclass(frozen=True)
class BrowserWindow:
    """Platform-neutral reference to a desktop window."""

    handle: object
    title: str


def _is_windows() -> bool:
    return sys.platform == "win32"


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def _xdotool(*args: str) -> str:
    """Run xdotool (Linux/X11) and return stdout; empty string if nothing matched."""
    import shutil
    import subprocess

    executable = shutil.which("xdotool")
    if not executable:
        raise RuntimeError(
            "xdotool is required on Linux to find and focus the browser window. "
            "Install it with: sudo apt install xdotool"
        )
    result = subprocess.run(
        [executable, *args], capture_output=True, text=True, timeout=10, check=False
    )
    return result.stdout.strip()


def ensure_desktop_automation_ready() -> None:
    """Fail early, with an actionable message, if the desktop cannot be automated."""
    if _is_windows():
        return
    if _is_linux():
        if (os.environ.get("XDG_SESSION_TYPE") or "").lower() == "wayland":
            raise RuntimeError(
                "This session is running Wayland, where PyAutoGUI and xdotool cannot "
                "control other windows. Log out and choose the X11 session "
                "(e.g. 'Lubuntu' / LXQt on X11) at the login screen."
            )
        if not os.environ.get("DISPLAY"):
            raise RuntimeError(
                "No DISPLAY found. Run the script from a desktop terminal session."
            )
        _xdotool("version")  # raises a clear error if xdotool is missing
        return
    raise RuntimeError(f"Unsupported platform for WhatsApp automation: {sys.platform}")


def list_whatsapp_windows() -> list[BrowserWindow]:
    """Return visible windows whose title mentions WhatsApp."""
    if _is_windows():
        import pygetwindow

        return [
            BrowserWindow(window, window.title)
            for window in pygetwindow.getAllWindows()
            if "whatsapp" in (window.title or "").lower()
        ]
    found: list[BrowserWindow] = []
    for window_id in _xdotool("search", "--onlyvisible", "--name", "[Ww]hats[Aa]pp").split():
        title = _xdotool("getwindowname", window_id)
        if "whatsapp" in title.lower():
            found.append(BrowserWindow(window_id, title))
    return found


def pick_browser_window(candidates: list[BrowserWindow]) -> BrowserWindow | None:
    if not candidates:
        return None
    return next(
        (
            item
            for item in candidates
            if any(word in item.title.lower() for word in BROWSER_WORDS)
        ),
        candidates[0],
    )


def activate_window(window: BrowserWindow) -> None:
    if _is_windows():
        handle = window.handle
        try:
            if handle.isMinimized:
                handle.restore()
            handle.activate()
        except Exception:
            # PyGetWindow often raises "Error code from Windows: 0 - The operation
            # completed successfully" even though focus changed. The caller
            # verifies the active window afterwards, so it is safe to ignore.
            pass
        return
    _xdotool("windowactivate", str(window.handle))
    _xdotool("windowfocus", str(window.handle))


def active_window_title() -> str:
    try:
        if _is_windows():
            import pygetwindow

            active = pygetwindow.getActiveWindow()
            return (active.title if active else "") or ""
        return _xdotool("getactivewindow", "getwindowname")
    except Exception:
        return ""


def window_geometry(window: BrowserWindow) -> tuple[int, int, int, int] | None:
    """Return left, top, width, height or None if it cannot be determined."""
    if _is_windows():
        handle = window.handle
        return handle.left, handle.top, handle.width, handle.height
    values: dict[str, int] = {}
    for line in _xdotool("getwindowgeometry", "--shell", str(window.handle)).splitlines():
        key, _, value = line.partition("=")
        if value.strip().lstrip("-").isdigit():
            values[key.strip()] = int(value)
    try:
        return values["X"], values["Y"], values["WIDTH"], values["HEIGHT"]
    except KeyError:
        return None


def whatsapp_is_focused() -> bool:
    return "whatsapp" in active_window_title().lower()


def focus_whatsapp_window(wait_seconds: int = WINDOW_SEARCH_SECONDS) -> BrowserWindow:
    """Find the WhatsApp browser window, bring it forward, and confirm focus."""
    deadline = time.monotonic() + wait_seconds
    window = None
    while True:
        window = pick_browser_window(list_whatsapp_windows())
        if window or time.monotonic() >= deadline:
            break
        time.sleep(1)
    if window is None:
        raise RuntimeError(
            "WhatsApp Web opened, but its browser window could not be found. "
            "Keep the browser visible and make it your default browser."
        )
    for _ in range(FOCUS_ATTEMPTS):
        activate_window(window)
        time.sleep(1.5)
        if whatsapp_is_focused():
            return window
        # The window handle can go stale if the browser re-creates the window.
        window = pick_browser_window(list_whatsapp_windows()) or window
    raise RuntimeError(
        "Could not bring the WhatsApp window to the front; no keys were pressed. "
        "Close extra browser windows and keep the desktop unlocked."
    )


def click_composer_area(window: BrowserWindow, pyautogui_module) -> None:
    """Optional click near the composer, clamped to the window and the screen."""
    geometry = window_geometry(window)
    if geometry is None:
        return
    left, top, width, height = geometry
    screen_width, screen_height = pyautogui_module.size()
    x = min(max(left + width // 2, 5), screen_width - 5)
    y = min(max(top + height - 65, 5), screen_height - 5)
    pyautogui_module.click(x, y)
    time.sleep(1)


def send_whatsapp(
    phone: str,
    message: str,
    wait_time: int,
    close_time: int,
    *,
    click_composer: bool = False,
    enter_presses: int = 2,
) -> None:
    """Open, focus, and submit a pre-filled WhatsApp Web message (Windows/Linux X11).

    The chat URL pre-fills the composer, and WhatsApp Web focuses it by itself,
    so by default no blind mouse click is made. The script confirms the WhatsApp
    window really has focus before pressing any key, presses Enter more than
    once (a second Enter on an empty composer is harmless) and only closes the
    tab if WhatsApp is still the active window.
    """
    ensure_desktop_automation_ready()
    import pyautogui

    recipient = phone.lstrip("+")
    url = (
        f"https://web.whatsapp.com/send?phone={quote(recipient)}"
        f"&text={quote(message)}"
    )
    if not webbrowser.open(url, new=2):
        raise RuntimeError("The default browser could not be opened.")

    time.sleep(wait_time)
    window = focus_whatsapp_window()
    time.sleep(1)

    if click_composer:
        click_composer_area(window, pyautogui)
        if not whatsapp_is_focused():
            raise RuntimeError("The composer click moved focus away from WhatsApp.")

    for attempt in range(max(enter_presses, 1)):
        if not whatsapp_is_focused():
            raise RuntimeError("WhatsApp lost focus before the message was submitted.")
        pyautogui.press("enter")
        if attempt + 1 < enter_presses:
            time.sleep(ENTER_PRESS_GAP_SECONDS)

    time.sleep(max(close_time, 2))
    if whatsapp_is_focused():
        pyautogui.hotkey("ctrl", "w")


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prepare",
        action="store_true",
        help="Validate phones and split eligible rows from review (after --strip-export)",
    )
    parser.add_argument(
        "--strip-export",
        action="store_true",
        help="Keep six CRM columns only and write <input-stem>_cleaned.csv",
    )
    parser.add_argument(
        "--strip-output",
        type=Path,
        help="Output path for --strip-export (default: <input-stem>_cleaned.csv)",
    )
    parser.add_argument("--input", type=Path, default=Path("prospects.csv"))
    parser.add_argument(
        "--cleaned-output",
        type=Path,
        help="Prepared eligible CSV (--prepare default: <input-stem>_prepared.csv)",
    )
    parser.add_argument("--campaign", help="Unique campaign name")
    parser.add_argument("--academy-name")
    parser.add_argument(
        "--messages",
        type=Path,
        help="JSON bank for Other, and the fallback when a country bank is not set",
    )
    parser.add_argument(
        "--messages-colombia",
        type=Path,
        help="Message bank used for Colombia instead of --messages",
    )
    parser.add_argument(
        "--messages-mexico",
        type=Path,
        help="Message bank used for Mexico instead of --messages",
    )
    parser.add_argument(
        "--messages-costa-rica",
        type=Path,
        help="Message bank used for Costa Rica instead of --messages",
    )
    parser.add_argument(
        "--messages-chile",
        type=Path,
        help="Message bank used for Chile instead of --messages",
    )
    parser.add_argument("--log", type=Path, default=Path("outreach_log.csv"))
    parser.add_argument("--review", type=Path, default=Path("review_queue.csv"))
    parser.add_argument("--do-not-contact", type=Path, default=Path("do_not_contact.csv"))
    parser.add_argument("--default-region", default="VC")
    parser.add_argument(
        "--contact-order",
        choices=(CONTACT_ORDER_OLDEST_FIRST, CONTACT_ORDER_FILE),
        default=CONTACT_ORDER_OLDEST_FIRST,
        help="Contact oldest signups first when date created is present (default)",
    )
    parser.add_argument(
        "--country",
        action="append",
        default=[],
        metavar="GROUP",
        help=(
            "Only contact these groups: colombia, mexico, costa_rica, chile, or other. "
            "Repeat the flag or separate names with commas."
        ),
    )
    parser.add_argument(
        "--date",
        help="Only contacts whose date created falls on this day (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--date-from",
        help="Only contacts created on or after this date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--date-to",
        help="Only contacts created on or before this date (YYYY-MM-DD)",
    )
    parser.add_argument("--max-messages", type=int, default=10)
    parser.add_argument("--delay", type=int, help="Use a fixed delay instead of random pacing")
    parser.add_argument("--min-delay", type=int, default=45)
    parser.add_argument("--max-delay", type=int, default=90)
    parser.add_argument("--wait-time", type=int, default=20)
    parser.add_argument("--close-time", type=int, default=3)
    parser.add_argument(
        "--enter-presses",
        type=int,
        default=2,
        help="Times Enter is pressed per message (extra presses are harmless; default 2)",
    )
    parser.add_argument(
        "--click-composer",
        action="store_true",
        help="Click near the message box before pressing Enter (off by default)",
    )
    parser.add_argument("--confirm-each", action="store_true")
    parser.add_argument("--send", action="store_true", help="Enable real WhatsApp Web automation")
    parser.add_argument("--yes", action="store_true", help="Skip the one-time SEND confirmation")
    parser.add_argument(
        "--ntfy-topic",
        default=os.environ.get("NTFY_TOPIC", ""),
        help="Optional ntfy topic for batch-completion notifications",
    )
    parser.add_argument(
        "--ntfy-server",
        default=os.environ.get("NTFY_SERVER", "https://ntfy.sh"),
        help="ntfy server URL (default: https://ntfy.sh)",
    )
    parser.add_argument(
        "--ntfy-token",
        default=os.environ.get("NTFY_TOKEN", ""),
        help="Optional ntfy access token; NTFY_TOKEN is preferred",
    )
    args = parser.parse_args(argv)
    if args.strip_export and args.prepare:
        parser.error("Use --strip-export and --prepare as separate steps, not together")
    if (args.prepare or args.strip_export) and (
        args.country
        or args.date
        or args.date_from
        or args.date_to
        or any(path for _, path in iter_country_message_paths(args))
    ):
        parser.error(
            "Country filters, date filters, and country message files are only used "
            "when previewing or sending"
        )
    if not args.prepare and not args.strip_export and (
        not args.campaign or not args.academy_name
    ):
        parser.error(
            "--campaign and --academy-name are required unless --prepare or "
            "--strip-export is used"
        )
    if args.max_messages < 1:
        parser.error("--max-messages must be at least 1")
    if args.delay is not None and args.delay < 0:
        parser.error("--delay cannot be negative")
    if args.min_delay < 0 or args.max_delay < args.min_delay:
        parser.error("--min-delay must be non-negative and not exceed --max-delay")
    if args.enter_presses < 1:
        parser.error("--enter-presses must be at least 1")
    if args.wait_time < 1 or args.close_time < 0:
        parser.error("delay/close-time cannot be negative and wait-time must be positive")
    if args.date and (args.date_from or args.date_to):
        parser.error("Use either --date or --date-from/--date-to, not both")
    try:
        args.country_groups = parse_country_selection(args.country)
        if args.date:
            args.date_from_dt = parse_filter_bound(args.date, end_of_day=False)
            args.date_to_dt = parse_filter_bound(args.date, end_of_day=True)
        else:
            args.date_from_dt = (
                parse_filter_bound(args.date_from, end_of_day=False) if args.date_from else None
            )
            args.date_to_dt = (
                parse_filter_bound(args.date_to, end_of_day=True) if args.date_to else None
            )
    except ValueError as exc:
        parser.error(str(exc))
    if (
        args.date_from_dt is not None
        and args.date_to_dt is not None
        and args.date_from_dt > args.date_to_dt
    ):
        parser.error("--date-from cannot be after --date-to")
    return args


def main(argv: Iterable[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else list(argv)
    if raw and raw[0] in {"--gui", "gui"}:
        if len(raw) != 1:
            print("The command maker does not take other arguments.", file=sys.stderr)
            print("Run: python outreach.py --gui", file=sys.stderr)
            return 2
        from command_maker import launch

        launch()
        return 0
    args = parse_args(raw)
    if not args.input.exists():
        print(f"Input file not found: {args.input}", file=sys.stderr)
        return 2
    if args.strip_export:
        output_path = args.strip_output or default_strip_output_path(args.input)
        try:
            row_count = strip_export_columns(args.input, output_path)
        except OSError as exc:
            print(f"Export strip failed: {exc}", file=sys.stderr)
            return 2
        prepared = default_prepare_cleaned_path(output_path)
        print("Column export completed; no phone validation or messages were sent.")
        print(f"Stripped export: {output_path} ({row_count} rows)")
        print("Next step:")
        print(f"  python outreach.py --prepare --input \"{output_path}\"")
        print(f"  (writes {prepared} by default; add --cleaned-output to rename)")
        return 0
    if args.prepare:
        cleaned_path = args.cleaned_output or default_prepare_cleaned_path(args.input)
        try:
            counts = prepare_dataset(
                args.input,
                cleaned_path,
                args.review,
                args.do_not_contact,
                args.default_region,
            )
        except (ValueError, OSError) as exc:
            print(f"Preparation failed: {exc}", file=sys.stderr)
            return 2
        print("Dataset preparation completed; no messages were sent.")
        print(f"Cleaned: {cleaned_path} (sorted oldest date created first)")
        print(f"Review: {args.review}")
        for status, count in sorted(counts.items()):
            print(f"  {status}: {count}")
        return 0

    rows, missing = read_csv(args.input)
    if missing:
        print(f"Missing required CSV columns: {', '.join(missing)}", file=sys.stderr)
        return 2
    oldest_first = args.contact_order == CONTACT_ORDER_OLDEST_FIRST
    if oldest_first:
        rows = sort_rows_by_date_created(rows, oldest_first=True)
        if rows_have_date_created(rows):
            print("Contact order: oldest date created first (longest wait first)")
        else:
            print(
                "Warning: no parseable date created in input; using file order.",
                file=sys.stderr,
            )
    else:
        print("Contact order: file order (as in the CSV)")
    filter_active = bool(args.country_groups or args.date_from_dt or args.date_to_dt)
    selected = [
        (row_number, row)
        for row_number, row in enumerate(rows, start=2)
        if row_matches_country(row, args.country_groups)
        and row_matches_date(row, args.date_from_dt, args.date_to_dt)
    ]
    if filter_active:
        print(
            "Contact filter: "
            + describe_contact_filter(
                args.country_groups, args.date_from or "", args.date_to or "", args.date or ""
            )
        )
        print(f"Selected rows: {len(selected)} of {len(rows)}")
        if not selected:
            print("No contacts match this country and date filter.")
            return 0
    print(f"Academy Outreach — {'REAL SEND' if args.send else 'DRY RUN'} — campaign: {args.campaign}")
    blocked = load_do_not_contact(args.do_not_contact, args.default_region)
    previously_sent = load_previous_sends(args.log, args.campaign)
    eligible_phones = eligible_phone_numbers(
        [row for _, row in selected], blocked, args.default_region
    )
    print_campaign_progress(eligible_phones, previously_sent)
    remaining_at_start = len(eligible_phones - previously_sent)
    run_target = min(args.max_messages, remaining_at_start)
    if args.send:
        print_run_progress(0, run_target)
    if args.send:
        try:
            ensure_desktop_automation_ready()
        except RuntimeError as exc:
            print(f"Cannot start real send: {exc}", file=sys.stderr)
            return 2
    if args.send and not args.yes:
        confirmation = input(f"REAL SEND is enabled (limit {args.max_messages}). Type SEND to continue: ")
        if confirmation != "SEND":
            print("Cancelled; no messages were sent.")
            return 1
    message_bank = None
    country_banks: dict[str, MessageBank] = {}
    try:
        if args.messages:
            message_bank = load_message_bank(args.messages)
        for group, path in iter_country_message_paths(args):
            if path:
                country_banks[group] = load_message_bank(path)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"Invalid message bank: {exc}", file=sys.stderr)
        return 2
    bank_parts = [f"other={args.messages}"] if args.messages else []
    bank_parts.extend(
        f"{COUNTRY_GROUP_LABELS[group]}={path}"
        for group, path in iter_country_message_paths(args)
        if path
    )
    if bank_parts:
        print("Message banks: " + ", ".join(bank_parts))
    delay_random = random.SystemRandom()
    seen: set[str] = set()
    sent_count = 0

    for row_number, row in selected:
        raw_phone = (row.get("phone") or "").strip()
        phone = normalize_phone(raw_phone, args.default_region) or ""
        prospect = Prospect(
            row_number=row_number,
            first_name=(row.get("first_name") or "").strip(),
            phone=phone or raw_phone,
            programme=(row.get("programme") or "").strip(),
            whatsapp_consent=(
                is_truthy(row.get("whatsapp_consent"))
                if "whatsapp_consent" in row
                else True
            ),
            opted_out=is_truthy(row.get("opt_out")),
            last_name=(row.get("last_name") or "").strip(),
            sign_up_comment=(row.get("sign_up_comment") or "").strip(),
            date_created=(row.get("date_created") or "").strip(),
            country=country_from_row(row),
        )
        if prospect.opted_out:
            log_result(args.log, args.campaign, prospect, "skipped_opt_out")
            continue
        if "whatsapp_consent" in row and not prospect.whatsapp_consent:
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
        if not prospect.first_name:
            reason = "missing first_name"
            add_to_review(args.review, args.campaign, prospect, reason)
            log_result(args.log, args.campaign, prospect, "human_review", error=reason)
            continue
        if sent_count >= args.max_messages:
            log_result(args.log, args.campaign, prospect, "skipped_session_limit")
            continue

        region = contact_region(row)
        group = region if region in PRIMARY_REGIONS else "other"
        bank = country_banks.get(group) or message_bank
        if bank:
            template_id, message = bank.next(prospect, args.academy_name)
        else:
            template_id, message = "default", generate_message(prospect, args.academy_name)
        planned_delay = (
            args.delay
            if args.delay is not None
            else delay_random.randint(args.min_delay, args.max_delay)
        )
        date_label = (prospect.date_created or "").strip()
        details = [f"{prospect.first_name} ({phone})"]
        if prospect.country:
            details.append(prospect.country)
        if date_label:
            details.append(f"date created: {date_label}")
        header = " — ".join(details)
        print(f"\n{header} [template: {template_id}]\n{message}")
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
            send_whatsapp(
                phone, message, args.wait_time, args.close_time,
                click_composer=args.click_composer,
                enter_presses=args.enter_presses,
            )
            sent_count += 1
            # Browser automation can confirm only that sending was requested.
            log_result(
                args.log, args.campaign, prospect, "send_requested", message,
                template_id=template_id, delay_seconds=planned_delay,
            )
            print_campaign_progress(eligible_phones, previously_sent, sent_count)
            print_run_progress(sent_count, run_target)
        except Exception as exc:
            log_result(
                args.log, args.campaign, prospect, "failed", message, str(exc),
                template_id=template_id,
            )
        if sent_count < args.max_messages:
            print(f"Waiting {planned_delay} seconds before the next send...")
            time.sleep(planned_delay)

    print(f"\nFinished. Real send requests this run: {sent_count}. Log: {args.log}")
    print_campaign_progress(eligible_phones, previously_sent, sent_count)
    if args.send:
        print_run_progress(sent_count, run_target)
    if args.send and args.ntfy_topic:
        reached, total, remaining, _ = campaign_progress(
            eligible_phones, previously_sent, sent_count
        )
        try:
            send_ntfy_notification(
                args.ntfy_server,
                args.ntfy_topic,
                args.campaign,
                sent_count,
                run_target,
                reached,
                total,
                remaining,
                args.ntfy_token,
            )
            print(f"ntfy notification sent to topic: {args.ntfy_topic}")
        except Exception as exc:
            print(f"Warning: ntfy notification failed: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
