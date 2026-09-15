import csv
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
