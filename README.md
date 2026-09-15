# Standalone Academy Outreach MVP

This repository contains a one-file alternative to the Django/OpenClaw application.
It reads prospective students from CSV, validates and deduplicates phone numbers,
applies consent and do-not-contact rules, creates a templated admissions message,
optionally sends it through WhatsApp Web with PyWhatKit, and logs every outcome.

The script is a desktop MVP. PyWhatKit controls WhatsApp Web in a graphical
browser; it is not the official WhatsApp Business API and cannot prove that a
recipient received a message. The log therefore uses `send_requested`, not
`delivered`. Use only with contacts who consented to the relevant outreach and
honour all opt-out requests.

## Requirements

- Python 3.11 or newer
- Chrome or another browser supported by PyWhatKit
- WhatsApp Web already linked and signed in
- A desktop session that remains unlocked while the script runs

## Windows setup

From PowerShell:

```powershell
git clone https://github.com/SoyJoseC/academy-outreach-standalone.git
cd academy-outreach-standalone
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item prospects.example.csv prospects.csv
Copy-Item do_not_contact.example.csv do_not_contact.csv
```

Edit `prospects.csv` with real, consented contacts. The required columns are:

| Column | Meaning |
|---|---|
| `first_name` | Name used in the message |
| `phone` | International/E.164 preferred, such as `+1784...` |
| `programme` | Programme of interest |
| `whatsapp_consent` | `yes` only when WhatsApp outreach was authorized |
| `opt_out` | `yes` prevents contact |

Put one blocked phone number per line in `do_not_contact.csv`. The header is
`phone`. Local numbers are interpreted as Saint Vincent and the Grenadines
(`VC`) unless `--default-region` is changed.

## 1. Preview without sending

Dry-run is the default and does not import PyWhatKit or open a browser:

```powershell
python outreach.py --campaign "2027-intake-test" --academy-name "Your Academy"
```

Review the terminal output and `outreach_log.csv`. Incomplete eligible records
are also written to `review_queue.csv` instead of being sent.

## 2. Send a controlled test

Start with your own number and a limit of one:

```powershell
python outreach.py --campaign "2027-intake-test" --academy-name "Your Academy" --send --max-messages 1 --confirm-each
```

The script asks you to type `SEND`, then asks before processing each message.
After confirming the browser workflow works, you may omit `--confirm-each` and
choose an appropriate limit and delay:

```powershell
python outreach.py --campaign "2027-intake" --academy-name "Your Academy" --send --max-messages 10 --delay 90
```

`--yes` suppresses the one-time `SEND` prompt and should be used only when you
have intentionally prepared and reviewed the input. A delay is an operational
rate control; it does not make unsolicited or bulk messaging compliant.

## Behaviour and safety controls

- Dry-run unless `--send` is supplied.
- Requires affirmative `whatsapp_consent`.
- Skips opt-outs and numbers in `do_not_contact.csv`.
- Normalizes and validates phone numbers using `phonenumbers`.
- Skips duplicates in the current CSV.
- Skips numbers already marked `send_requested` for the same campaign.
- Sends incomplete records to `review_queue.csv`.
- Enforces a per-run message limit and inter-message delay.
- Records skipped, dry-run, failed, and requested-send results.

Use a new unique `--campaign` value for a genuinely different campaign. Reusing
the same name intentionally prevents the same number being sent twice after a
successful automation request.

## Useful options

```text
--input PATH             Input CSV (default: prospects.csv)
--log PATH               Audit CSV (default: outreach_log.csv)
--review PATH            Human-review CSV (default: review_queue.csv)
--do-not-contact PATH     Block-list CSV (default: do_not_contact.csv)
--default-region CODE     Region for local phone numbers (default: VC)
--max-messages NUMBER     Maximum real send requests per run (default: 10)
--delay SECONDS           Wait between real send requests (default: 60)
--confirm-each            Require approval for each eligible message
--send                    Enable real browser automation
--yes                     Skip the one-time SEND confirmation
```

## Limitations

- No inbound reply or automatic `STOP` processing; update the CSV/block list.
- No reliable delivery/read receipts.
- WhatsApp Web UI changes can break browser automation.
- The machine must remain signed in and attended.
- CSV files are not a multi-user database and should be access-controlled.
- Replace this with an approved business messaging integration before
  unattended or larger-scale production use.

## Run tests

Tests do not open WhatsApp or send messages:

```powershell
python -m unittest discover -s tests -v
```
