# Standalone Academy Outreach MVP

This repository contains a one-file alternative to the Django/OpenClaw application.
It prepares prospective-student data, validates and deduplicates phone numbers,
applies consent and do-not-contact rules, rotates a user-written message bank,
optionally sends through controlled WhatsApp Web browser automation, and logs
every outcome.

The script is a desktop MVP. It uses the same graphical-browser approach as
PyWhatKit, with an explicit Windows focus step to avoid the common situation in
which the message is prepared but Enter is sent to the wrong window. This is not
the official WhatsApp Business API and cannot prove that a recipient received a
message. The log therefore uses `send_requested`, not `delivered`. Use only with
contacts who consented to the relevant outreach and honour all opt-out requests.

## Requirements

- Python 3.11 or newer
- Chrome, Edge, Firefox, Brave, or Opera configured as the default browser
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

Edit `prospects.csv` with contacts who previously initiated an admissions
inquiry. The accepted source columns are:

| Column | Meaning |
|---|---|
| `first_name` | Name used in the message |
| `last_name` | Optional family name |
| `phone` | International or national-format number |
| `country` | Country name or ISO code used when the calling code is missing |
| `sign_up_comment` | Optional context from the original inquiry |
| `opt_out` | Optional; `yes` prevents contact |

Headers containing spaces, including `first name`, `last name`, and
`sign up comment`, are accepted directly. `programme` and `whatsapp_consent`
remain optional for compatibility with older files, but are not required. When
`whatsapp_consent` is present, a negative value is still respected.

Put one blocked phone number per line in `do_not_contact.csv`. The header is
`phone`. Block-list numbers should use complete international format.

## 1. Prepare and review the dataset

Never send directly from an uncleaned export. Copy the example and place the
raw data in `prospects.csv`, then run:

```powershell
python outreach.py --prepare --input prospects.csv
```

Preparation never opens a browser or sends a message. It creates:

- `cleaned_prospects.csv`: eligible, deduplicated contacts with E.164 numbers.
- `review_queue.csv`: invalid, ambiguous, duplicate, opted-out, no-consent, and
  do-not-contact rows with a reason.

Spanish country names and accents are supported. The primary mappings include
Colombia (`CO`), Costa Rica (`CR`), Mexico (`MX`), and Chile (`CL`), plus the
other Spanish-speaking Latin American countries. International numbers are
cross-checked against the stated country; the program does not guess when they
disagree.

Validation confirms that a number fits a numbering plan. It cannot confirm that
the number is currently assigned or registered with WhatsApp.

## 2. Configure the message bank

Copy `messages.example.json` to `messages.json` and replace the example with at
least ten objects. Messages are authored by the operator; the program does not
invent academy claims. The supported placeholders are `{first_name}`,
`{last_name}`, `{sign_up_comment}`, `{programme}`, and `{academy_name}`. The
`programme` placeholder is available for older or enriched datasets; an initial
contact template does not need to use it.

```json
{
  "messages": [
    {"id": "admissions_01", "text": "YOUR FIRST MESSAGE"},
    {"id": "admissions_02", "text": "YOUR SECOND MESSAGE"}
  ]
}
```

The file must contain at least ten unique IDs. Set `opt_out_text` once at the top
of the file in the language used by the campaign. The program shuffles the bank,
uses every message once per cycle, reshuffles, appends that standard opt-out
line, and records the selected ID in `outreach_log.csv`.

## 3. Preview without sending

Dry-run is the default and does not load the desktop automation libraries or
open a browser:

```powershell
python outreach.py --input cleaned_prospects.csv --messages messages.json --campaign "2027-intake-test" --academy-name "Your Academy"
```

Review the terminal output and `outreach_log.csv`. Incomplete eligible records
are also written to `review_queue.csv` instead of being sent.

The preview prints and records the randomly selected delay for each contact.

## 4. Send a controlled test

Start with your own number and a limit of one:

```powershell
python outreach.py --input cleaned_prospects.csv --messages messages.json --campaign "2027-intake-test" --academy-name "Your Academy" --send --max-messages 1 --confirm-each
```

The script asks you to type `SEND`, then asks before processing each message.
After confirming the browser workflow works, you may omit `--confirm-each` and
choose an appropriate limit and delay:

```powershell
python outreach.py --input cleaned_prospects.csv --messages messages.json --campaign "2027-intake" --academy-name "Your Academy" --send --max-messages 10 --min-delay 45 --max-delay 90
```

`--yes` suppresses the one-time `SEND` prompt and should be used only when you
have intentionally prepared and reviewed the input. A delay is an operational
rate control; it does not make unsolicited or bulk messaging compliant.

To receive an ntfy notification when a real-send batch finishes, add your topic:

```powershell
python outreach.py --input cleaned_prospects.csv --messages messages.json --campaign "2027-intake" --academy-name "Your Academy" --send --max-messages 30 --ntfy-topic "your-private-topic"
```

Subscribe to that same topic in the ntfy mobile app. For a protected topic, set
the token in PowerShell before running the command:

```powershell
$env:NTFY_TOKEN = "your-access-token"
```

Self-hosted ntfy users can add `--ntfy-server "https://ntfy.example.com"`.
Notification failures produce a warning but do not change the batch results.

## Behaviour and safety controls

- Dry-run unless `--send` is supplied.
- Treats inclusion in the curated input as an existing inquiry; if the optional
  `whatsapp_consent` column is present, negative values are respected.
- Skips opt-outs and numbers in `do_not_contact.csv`.
- Normalizes and validates phone numbers using `phonenumbers`.
- Corrects missing calling codes using a known country.
- Skips duplicates in the current CSV.
- Skips numbers already marked `send_requested` for the same campaign.
- Shows campaign progress across batches, for example
  `Campaign progress: 40/420 reached (9.5%) — 380 remaining`.
- Shows current-run progress after every requested send, for example
  `Run progress: 1/30 contacted this run`.
- Optionally sends an ntfy notification after a real-send batch finishes.
- Sends incomplete records to `review_queue.csv`.
- Enforces a per-run message limit and inter-message delay.
- Uses each message-bank entry once before reshuffling the bank.
- Records skipped, dry-run, failed, and requested-send results.

Use a new unique `--campaign` value for a genuinely different campaign. Reusing
the same name intentionally prevents the same number being sent twice after a
successful automation request.

The progress total counts unique contacts in the current input that are eligible
to receive a message. The reached count includes only numbers recorded as
`send_requested` for that campaign. Keep the same campaign name, log file, and
input list across batches so the counter remains consistent. Dry runs and failed
attempts do not increase the reached count.

## Useful options

```text
--prepare                Clean the dataset and exit without sending
--input PATH             Input CSV (default: prospects.csv)
--cleaned-output PATH    Prepared eligible CSV (default: cleaned_prospects.csv)
--messages PATH          JSON bank containing at least 10 messages
--log PATH               Audit CSV (default: outreach_log.csv)
--review PATH            Human-review CSV (default: review_queue.csv)
--do-not-contact PATH     Block-list CSV (default: do_not_contact.csv)
--default-region CODE     Fallback region for the do-not-contact list (default: VC)
--max-messages NUMBER     Maximum real send requests per run (default: 10)
--min-delay SECONDS       Minimum random delay (default: 45)
--max-delay SECONDS       Maximum random delay (default: 90)
--delay SECONDS           Optional fixed-delay compatibility override
--confirm-each            Require approval for each eligible message
--send                    Enable real browser automation
--yes                     Skip the one-time SEND confirmation
--ntfy-topic TOPIC        Notify this ntfy topic when a real batch finishes
--ntfy-server URL         ntfy server (default: https://ntfy.sh)
--ntfy-token TOKEN        Optional access token (prefer NTFY_TOKEN environment variable)
```

## Limitations

- No inbound reply or automatic `STOP` processing; update the CSV/block list.
- No reliable delivery/read receipts.
- WhatsApp Web UI changes can break browser automation.
- The machine must remain signed in and attended.
- The current focus controller is intended for Windows desktop use.
- CSV files are not a multi-user database and should be access-controlled.
- Replace this with an approved business messaging integration before
  unattended or larger-scale production use.

## Run tests

Tests do not open WhatsApp or send messages:

```powershell
python -m unittest discover -s tests -v
```
