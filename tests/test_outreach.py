import csv
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import outreach


class OutreachTests(unittest.TestCase):
    def test_truthy_values(self):
        self.assertTrue(outreach.is_truthy(" YES "))
        self.assertFalse(outreach.is_truthy("no"))

    def test_normalizes_svg_number(self):
        self.assertEqual(outreach.normalize_phone("784 456 1111", "VC"), "+17844561111")

    def test_invalid_phone_returns_none(self):
        self.assertIsNone(outreach.normalize_phone("not-a-phone", "VC"))

    def test_resolves_spanish_country_names_and_accents(self):
        self.assertEqual(outreach.resolve_country_region("México"), "MX")
        self.assertEqual(outreach.resolve_country_region("República Dominicana"), "DO")

    def test_adds_country_calling_codes_to_primary_markets(self):
        cases = [
            ("3001234567", "Colombia", "+573001234567"),
            ("83123456", "Costa Rica", "+50683123456"),
            ("5512345678", "México", "+525512345678"),
            ("987654321", "Chile", "+56987654321"),
        ]
        for raw, country, expected in cases:
            with self.subTest(country=country):
                normalized, status, reason, _ = outreach.normalize_phone_from_country(
                    raw, country
                )
                self.assertEqual(normalized, expected)
                self.assertEqual(status, "corrected_from_country")
                self.assertEqual(reason, "")

    def test_country_mismatch_requires_review(self):
        normalized, status, _, detected = outreach.normalize_phone_from_country(
            "+573001234567", "México"
        )
        self.assertIsNone(normalized)
        self.assertEqual(status, "country_mismatch")
        self.assertEqual(detected, "CO")

    def test_message_bank_uses_every_message_before_repeating(self):
        messages = [
            {"id": f"message_{number}", "text": "Hello {first_name}"}
            for number in range(10)
        ]
        bank = outreach.MessageBank(messages)
        prospect = outreach.Prospect(2, "Jose", "+573001234567", "IT", True, False)
        selected = [bank.next(prospect, "Academy")[0] for _ in range(10)]
        self.assertEqual(len(set(selected)), 10)

    def test_message_bank_requires_ten_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "messages.json"
            path.write_text(json.dumps({"messages": [{"id": "one", "text": "Hi"}]}))
            with self.assertRaisesRegex(ValueError, "at least 10"):
                outreach.load_message_bank(path)

    def test_prepare_separates_clean_and_review_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "raw.csv"
            clean = root / "clean.csv"
            review = root / "review.csv"
            source.write_text(
                "first_name,phone,country,programme,whatsapp_consent,opt_out\n"
                "Ana,3001234567,Colombia,Computing,yes,no\n"
                "Luis,3001234567,Colombia,Business,yes,no\n"
                "Eva,not-a-phone,Chile,Nursing,yes,no\n",
                encoding="utf-8",
            )
            counts = outreach.prepare_dataset(
                source, clean, review, root / "dnc.csv", "VC"
            )
            with clean.open(newline="", encoding="utf-8") as handle:
                clean_rows = list(csv.DictReader(handle))
            with review.open(newline="", encoding="utf-8") as handle:
                review_rows = list(csv.DictReader(handle))
            self.assertEqual(clean_rows[0]["phone"], "+573001234567")
            self.assertEqual({row["validation_status"] for row in review_rows}, {"duplicate", "invalid_number"})
            self.assertEqual(counts["corrected_from_country"], 1)

    def test_prepare_accepts_actual_contact_columns_without_consent_or_programme(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "raw.csv"
            clean = root / "clean.csv"
            review = root / "review.csv"
            source.write_text(
                "first name,last name,phone,country,sign up comment\n"
                "Ana,Lopez,3001234567,Colombia,Requested information\n",
                encoding="utf-8",
            )
            result = outreach.prepare_dataset(
                source, clean, review, root / "dnc.csv", "VC"
            )
            with clean.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(result["corrected_from_country"], 1)
            self.assertEqual(rows[0]["first_name"], "Ana")
            self.assertEqual(rows[0]["last_name"], "Lopez")
            self.assertEqual(rows[0]["sign_up_comment"], "Requested information")

    def test_cleaned_contact_without_programme_can_be_previewed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "cleaned.csv"
            log_path = root / "log.csv"
            input_path.write_text(
                "first_name,last_name,phone,country,sign_up_comment,opt_out\n"
                "Ana,Lopez,+573001234567,Colombia,Requested information,no\n",
                encoding="utf-8",
            )
            with patch.object(outreach, "send_whatsapp") as sender:
                result = outreach.main([
                    "--input", str(input_path),
                    "--log", str(log_path),
                    "--review", str(root / "review.csv"),
                    "--do-not-contact", str(root / "dnc.csv"),
                    "--campaign", "first-contact",
                    "--academy-name", "Test Academy",
                ])
            self.assertEqual(result, 0)
            sender.assert_not_called()
            with log_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["status"], "dry_run")
            self.assertNotIn("interest in .", rows[0]["message"])

    def test_dry_run_logs_but_never_sends(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "prospects.csv"
            log_path = root / "log.csv"
            input_path.write_text(
                "first_name,phone,programme,whatsapp_consent,opt_out\n"
                "Jose,+17844561111,Computing,yes,no\n",
                encoding="utf-8",
            )
            with patch.object(outreach, "send_whatsapp") as sender:
                result = outreach.main([
                    "--input", str(input_path),
                    "--log", str(log_path),
                    "--review", str(root / "review.csv"),
                    "--do-not-contact", str(root / "dnc.csv"),
                    "--campaign", "test",
                    "--academy-name", "Test Academy",
                ])
            self.assertEqual(result, 0)
            sender.assert_not_called()
            with log_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["status"], "dry_run")

    def test_campaign_progress_counts_only_unique_eligible_contacts(self):
        rows = [
            {"first_name": "Ana", "phone": "+573001234567", "opt_out": "no"},
            {"first_name": "Ana duplicate", "phone": "+57 300 123 4567", "opt_out": "no"},
            {"first_name": "Luis", "phone": "+50683123456", "opt_out": "yes"},
            {"first_name": "Eva", "phone": "+525512345678", "opt_out": "no"},
            {"first_name": "", "phone": "+56987654321", "opt_out": "no"},
        ]
        eligible = outreach.eligible_phone_numbers(
            rows, {"+525512345678"}, "VC"
        )
        self.assertEqual(eligible, {"+573001234567"})
        self.assertEqual(
            outreach.campaign_progress(eligible, {"+573001234567"}),
            (1, 1, 0, 100.0),
        )

    def test_progress_continues_across_real_send_batches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "prospects.csv"
            log_path = root / "log.csv"
            input_path.write_text(
                "first_name,phone,opt_out\n"
                "Ana,+573001234567,no\n"
                "Luis,+50683123456,no\n"
                "Eva,+525512345678,no\n",
                encoding="utf-8",
            )
            common_args = [
                "--input", str(input_path),
                "--log", str(log_path),
                "--review", str(root / "review.csv"),
                "--do-not-contact", str(root / "dnc.csv"),
                "--campaign", "progress-test",
                "--academy-name", "Test Academy",
                "--send", "--yes", "--delay", "0",
            ]
            with patch.object(outreach, "send_whatsapp"), redirect_stdout(StringIO()) as first:
                self.assertEqual(outreach.main(common_args + ["--max-messages", "2"]), 0)
            self.assertIn("Campaign progress: 2/3 reached", first.getvalue())
            self.assertIn("Run progress: 2/2 contacted this run", first.getvalue())

            with patch.object(outreach, "send_whatsapp"), redirect_stdout(StringIO()) as second:
                self.assertEqual(outreach.main(common_args + ["--max-messages", "2"]), 0)
            self.assertIn("Campaign progress: 3/3 reached", second.getvalue())
            self.assertIn("Run progress: 1/1 contacted this run", second.getvalue())

    def test_real_batch_sends_ntfy_completion_notification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "prospects.csv"
            input_path.write_text(
                "first_name,phone,opt_out\nAna,+573001234567,no\n",
                encoding="utf-8",
            )
            with (
                patch.object(outreach, "send_whatsapp"),
                patch.object(outreach, "send_ntfy_notification") as notify,
                redirect_stdout(StringIO()) as output,
            ):
                result = outreach.main([
                    "--input", str(input_path),
                    "--log", str(root / "log.csv"),
                    "--review", str(root / "review.csv"),
                    "--do-not-contact", str(root / "dnc.csv"),
                    "--campaign", "ntfy-test",
                    "--academy-name", "Test Academy",
                    "--send", "--yes", "--max-messages", "30",
                    "--delay", "0", "--ntfy-topic", "academy-test",
                ])
            self.assertEqual(result, 0)
            notify.assert_called_once_with(
                "https://ntfy.sh", "academy-test", "ntfy-test",
                1, 1, 1, 1, 0, "",
            )
            self.assertIn("Run progress: 1/1 contacted this run", output.getvalue())


if __name__ == "__main__":
    unittest.main()
