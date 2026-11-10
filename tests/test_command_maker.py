import unittest
from pathlib import Path

import command_maker
import outreach


class CommandMakerTests(unittest.TestCase):
    def test_country_and_date_flags_parse(self):
        args = command_maker.build_outreach_args(
            {
                "mode": "send",
                "input": "people.csv",
                "campaign": "2027",
                "academy_name": "Academy",
                "countries": ["colombia", "costa_rica"],
                "date_mode": "range",
                "date_from": "2026-09-01",
                "date_to": "2026-09-30",
                "messages": "messages.json",
                "messages_chile": "messages_cl.json",
            }
        )
        parsed = outreach.parse_args(args)
        self.assertEqual(parsed.country_groups, {"CO", "CR"})
        self.assertEqual(parsed.date_from, "2026-09-01")
        self.assertEqual(parsed.date_to, "2026-09-30")
        self.assertEqual(parsed.messages, Path("messages.json"))
        self.assertEqual(parsed.messages_chile, Path("messages_cl.json"))
        self.assertFalse(parsed.send)
        self.assertEqual(parsed.max_messages, 10)
        self.assertEqual(parsed.min_delay, 45)
        self.assertEqual(parsed.max_delay, 90)

    def test_on_date_ignores_leftover_range_fields(self):
        args = command_maker.build_outreach_args(
            {
                "mode": "send",
                "campaign": "c",
                "academy_name": "Academy",
                "date_mode": "on",
                "date": "2026-09-20",
                "date_from": "2026-10-01",
            }
        )
        parsed = outreach.parse_args(args)
        self.assertEqual(parsed.date, "2026-09-20")
        self.assertIsNone(parsed.date_from)

    def test_strip_omits_outreach_flags(self):
        args = command_maker.build_outreach_args(
            {
                "mode": "strip",
                "input": "export.csv",
                "campaign": "ignored",
                "countries": ["chile"],
            }
        )
        self.assertEqual(args[:2], ["--strip-export", "--input"])
        self.assertNotIn("--country", args)
        self.assertNotIn("--campaign", args)
        parsed = outreach.parse_args(args)
        self.assertTrue(parsed.strip_export)

    def test_quotes_paths_with_spaces(self):
        self.assertEqual(
            command_maker.format_command(["--input", "my file.csv", "--country", "colombia"]),
            'python outreach.py --input "my file.csv" --country colombia',
        )

    def test_send_and_yes_are_included_together(self):
        args = command_maker.build_outreach_args(
            {
                "mode": "send",
                "campaign": "c",
                "academy_name": "Academy",
                "send": True,
                "yes": True,
                "countries": ["other"],
            }
        )
        self.assertIn("--send", args)
        self.assertIn("--yes", args)
        self.assertIn("other", args)
        parsed = outreach.parse_args(args)
        self.assertTrue(parsed.send)
        self.assertTrue(parsed.yes)
        self.assertEqual(parsed.country_groups, {"other"})

    def test_yes_without_send_is_omitted(self):
        args = command_maker.build_outreach_args(
            {
                "mode": "send",
                "campaign": "c",
                "academy_name": "Academy",
                "send": False,
                "yes": True,
            }
        )
        self.assertNotIn("--yes", args)
        self.assertNotIn("--send", args)

    def test_rejects_inverted_dates_and_missing_names(self):
        with self.assertRaisesRegex(ValueError, "after"):
            command_maker.build_outreach_args(
                {
                    "mode": "send",
                    "campaign": "c",
                    "academy_name": "Academy",
                    "date_mode": "range",
                    "date_from": "2026-10-01",
                    "date_to": "2026-09-01",
                }
            )
        with self.assertRaisesRegex(ValueError, "Campaign"):
            command_maker.build_outreach_args({"mode": "send", "input": "people.csv"})

    def test_describe_names_the_selected_countries(self):
        text = command_maker.describe_command(
            {
                "mode": "send",
                "countries": ["mexico", "chile"],
                "send": True,
                "date_mode": "on",
                "date": "2026-09-20",
            }
        )
        self.assertIn("Send real WhatsApp messages", text)
        self.assertIn("Mexico", text)
        self.assertIn("Chile", text)
        self.assertIn("2026-09-20", text)


if __name__ == "__main__":
    unittest.main()
