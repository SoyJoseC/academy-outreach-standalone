#!/usr/bin/env python3
"""Tkinter window that builds, copies, and runs an outreach.py command."""

from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
OUTREACH_SCRIPT = PROJECT_DIR / "outreach.py"


def project_python() -> str:
    """Prefer the project virtual environment so Run has the outreach packages."""
    folder = "Scripts" if sys.platform == "win32" else "bin"
    candidate = PROJECT_DIR / ".venv" / folder / ("python.exe" if sys.platform == "win32" else "python")
    if candidate.exists():
        return str(candidate)
    return sys.executable

COUNTRY_FLAGS = (
    ("colombia", "Colombia"),
    ("mexico", "Mexico"),
    ("costa_rica", "Costa Rica"),
    ("chile", "Chile"),
    ("other", "Other"),
)
MESSAGE_FLAGS = (
    ("messages", "--messages"),
    ("messages_colombia", "--messages-colombia"),
    ("messages_mexico", "--messages-mexico"),
    ("messages_costa_rica", "--messages-costa-rica"),
    ("messages_chile", "--messages-chile"),
)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}$")
_SIMPLE_ARG = re.compile(r"[A-Za-z0-9_./:\\=+@,%:-]+")

# Keep these aligned with outreach.parse_args defaults.
DEFAULT_OPTIONS = {
    "mode": "send",
    "input": "prospects.csv",
    "strip_output": "",
    "cleaned_output": "",
    "campaign": "",
    "academy_name": "",
    "messages": "",
    "messages_colombia": "",
    "messages_mexico": "",
    "messages_costa_rica": "",
    "messages_chile": "",
    "countries": [],
    "date_mode": "none",
    "date": "",
    "date_from": "",
    "date_to": "",
    "log": "outreach_log.csv",
    "review": "review_queue.csv",
    "do_not_contact": "do_not_contact.csv",
    "default_region": "VC",
    "contact_order": "oldest-first",
    "max_messages": "10",
    "min_delay": "45",
    "max_delay": "90",
    "delay": "",
    "wait_time": "20",
    "close_time": "3",
    "enter_presses": "2",
    "click_composer": False,
    "confirm_each": False,
    "send": False,
    "yes": False,
    "ntfy_topic": "",
    "ntfy_server": "https://ntfy.sh",
    "ntfy_token": "",
    "include_ntfy_token": False,
}


def _merged(options: dict) -> dict:
    merged = {**DEFAULT_OPTIONS, **options}
    countries = merged.get("countries") or []
    if isinstance(countries, str):
        countries = [part.strip() for part in countries.split(",") if part.strip()]
    merged["countries"] = list(countries)
    return merged


def _text(options: dict, key: str) -> str:
    return str(options.get(key) or "").strip()


def _whole(value: str, label: str, minimum: int) -> str:
    if not re.fullmatch(r"\d+", value or ""):
        raise ValueError(f"{label} must be a whole number")
    number = int(value)
    if number < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    return str(number)


def _require_date(value: str, label: str) -> str:
    if not _DATE.fullmatch(value or ""):
        raise ValueError(f"{label} must use YYYY-MM-DD")
    return value


def build_outreach_args(options: dict) -> list[str]:
    """Return outreach.py arguments, without the Python executable."""
    opts = _merged(options)
    mode = opts["mode"]
    if mode not in {"strip", "prepare", "send"}:
        raise ValueError("Function must be strip, prepare, or send")
    source = _text(opts, "input")
    if not source:
        raise ValueError("Choose an input CSV")

    if mode == "strip":
        args = ["--strip-export", "--input", source]
        if _text(opts, "strip_output"):
            args.extend(["--strip-output", _text(opts, "strip_output")])
        return args

    if mode == "prepare":
        args = ["--prepare", "--input", source]
        if _text(opts, "cleaned_output"):
            args.extend(["--cleaned-output", _text(opts, "cleaned_output")])
        args.extend(
            [
                "--review",
                _text(opts, "review") or DEFAULT_OPTIONS["review"],
                "--do-not-contact",
                _text(opts, "do_not_contact") or DEFAULT_OPTIONS["do_not_contact"],
                "--default-region",
                _text(opts, "default_region") or DEFAULT_OPTIONS["default_region"],
            ]
        )
        return args

    campaign = _text(opts, "campaign")
    academy = _text(opts, "academy_name")
    if not campaign or not academy:
        raise ValueError("Campaign and academy name are required for outreach")
    unknown = [name for name in opts["countries"] if name not in {key for key, _ in COUNTRY_FLAGS}]
    if unknown:
        raise ValueError("Unknown country: " + ", ".join(unknown))

    args = ["--input", source]
    for key, flag in MESSAGE_FLAGS:
        path = _text(opts, key)
        if path:
            args.extend([flag, path])
    selected = [key for key, _label in COUNTRY_FLAGS if key in opts["countries"]]
    if selected:
        args.extend(["--country", ",".join(selected)])

    date_mode = opts["date_mode"] or "none"
    if date_mode == "on":
        args.extend(["--date", _require_date(_text(opts, "date"), "Date")])
    elif date_mode == "range":
        start = _text(opts, "date_from")
        end = _text(opts, "date_to")
        if not start and not end:
            raise ValueError("Enter a from date, a to date, or both")
        if start:
            args.extend(["--date-from", _require_date(start, "Date from")])
        if end:
            args.extend(["--date-to", _require_date(end, "Date to")])
        if start and end and start > end:
            raise ValueError("Date from cannot be after date to")
    elif date_mode != "none":
        raise ValueError("Date filter must be none, on, or range")

    args.extend(
        [
            "--campaign",
            campaign,
            "--academy-name",
            academy,
            "--log",
            _text(opts, "log") or DEFAULT_OPTIONS["log"],
            "--review",
            _text(opts, "review") or DEFAULT_OPTIONS["review"],
            "--do-not-contact",
            _text(opts, "do_not_contact") or DEFAULT_OPTIONS["do_not_contact"],
            "--default-region",
            _text(opts, "default_region") or DEFAULT_OPTIONS["default_region"],
            "--contact-order",
            _text(opts, "contact_order") or DEFAULT_OPTIONS["contact_order"],
            "--max-messages",
            _whole(_text(opts, "max_messages"), "Maximum messages", 1),
        ]
    )
    delay = _text(opts, "delay")
    if delay:
        args.extend(["--delay", _whole(delay, "Fixed delay", 0)])
    else:
        minimum = _whole(_text(opts, "min_delay"), "Minimum delay", 0)
        maximum = _whole(_text(opts, "max_delay"), "Maximum delay", 0)
        if int(minimum) > int(maximum):
            raise ValueError("Minimum delay cannot exceed maximum delay")
        args.extend(["--min-delay", minimum, "--max-delay", maximum])
    args.extend(
        [
            "--wait-time",
            _whole(_text(opts, "wait_time"), "Wait time", 1),
            "--close-time",
            _whole(_text(opts, "close_time"), "Close time", 0),
            "--enter-presses",
            _whole(_text(opts, "enter_presses"), "Enter presses", 1),
        ]
    )
    if opts.get("click_composer"):
        args.append("--click-composer")
    if opts.get("confirm_each"):
        args.append("--confirm-each")
    if opts.get("send"):
        args.append("--send")
        if opts.get("yes"):
            args.append("--yes")
    topic = _text(opts, "ntfy_topic")
    if topic:
        args.extend(["--ntfy-topic", topic])
        server = _text(opts, "ntfy_server")
        if server:
            args.extend(["--ntfy-server", server])
        if opts.get("include_ntfy_token") and _text(opts, "ntfy_token"):
            args.extend(["--ntfy-token", _text(opts, "ntfy_token")])
    return args


def quote_arg(value: str) -> str:
    if _SIMPLE_ARG.fullmatch(value):
        return value
    escaped = value.replace("`", "``").replace('"', '`"')
    return f'"{escaped}"'


def format_command(args: list[str]) -> str:
    return " ".join(["python", "outreach.py", *(quote_arg(arg) for arg in args)])


def describe_command(options: dict) -> str:
    opts = _merged(options)
    if opts["mode"] == "strip":
        return "Strip the export to the six outreach columns. No messages are sent."
    if opts["mode"] == "prepare":
        return "Validate phone numbers and write the prepared list. No messages are sent."
    labels = [label for key, label in COUNTRY_FLAGS if key in opts["countries"]]
    who = ", ".join(labels) if labels else "all countries"
    action = "Send real WhatsApp messages" if opts.get("send") else "Preview messages without sending"
    if opts["date_mode"] == "on" and _text(opts, "date"):
        when = f" Date created: {_text(opts, 'date')}."
    elif opts["date_mode"] == "range" and (_text(opts, "date_from") or _text(opts, "date_to")):
        start = _text(opts, "date_from") or "…"
        end = _text(opts, "date_to") or "…"
        when = f" Date created: {start} to {end}."
    else:
        when = ""
    return f"{action}. Contact {who}.{when}"


def launch() -> None:
    try:
        CommandMakerApp().run()
    except ImportError:
        print(
            "This Python does not include Tkinter, so the window cannot open.\n"
            "On Windows, start it with a Python that has Tk:\n"
            "  py command_maker.py\n"
            "Run still uses the project .venv. On Linux: sudo apt install python3-tk",
            file=sys.stderr,
        )
        raise SystemExit(1)


class CommandMakerApp:
    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import filedialog, messagebox, scrolledtext, ttk

        self.tk = tk
        self.ttk = ttk
        self.filedialog = filedialog
        self.messagebox = messagebox
        self.scrolledtext = scrolledtext
        self.root = tk.Tk()
        self.root.title("Academy Outreach command maker")
        self.root.minsize(760, 560)
        self.root.geometry("860x760")
        self._ready = False
        self._args: list[str] | None = None
        self._process: subprocess.Popen[str] | None = None
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._controls: list[tuple[object, frozenset[str], str]] = []
        self.vars: dict[str, tk.Variable] = {}
        self.country_vars: dict[str, tk.BooleanVar] = {}
        self._build()
        self._wire_traces()
        self._ready = True
        self._refresh()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def run(self) -> None:
        self.root.mainloop()

    def _var(self, name: str, value: str):
        variable = self.tk.StringVar(value=value)
        self.vars[name] = variable
        return variable

    def _bool_var(self, name: str, value: bool = False):
        variable = self.tk.BooleanVar(value=value)
        self.vars[name] = variable
        return variable

    def _track(self, widget, modes: set[str], enabled: str = "normal") -> None:
        self._controls.append((widget, frozenset(modes), enabled))

    def _build(self) -> None:
        tk = self.tk
        ttk = self.ttk
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        self.root.rowconfigure(5, weight=1)

        header = ttk.Frame(self.root, padding=(12, 10, 12, 0))
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="Academy Outreach command maker", font=("", 14, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            header,
            text=(
                "Choose a function, set the flags, then copy or run the command. "
                "Copy expects this folder, with .venv activated. Run uses .venv directly."
            ),
        ).grid(row=1, column=0, sticky="w")

        canvas = tk.Canvas(self.root, highlightthickness=0)
        canvas.grid(row=1, column=0, sticky="nsew", padx=(12, 0))
        scroll = ttk.Scrollbar(self.root, orient="vertical", command=canvas.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        canvas.configure(yscrollcommand=scroll.set)
        form = ttk.Frame(canvas, padding=(0, 0, 12, 8))
        form_window = canvas.create_window((0, 0), window=form, anchor="nw")
        form.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(form_window, width=event.width))
        canvas.bind_all("<MouseWheel>", lambda event: canvas.yview_scroll(int(-event.delta / 120), "units"))
        self.canvas = canvas
        form.columnconfigure(1, weight=1)

        row = self._build_function(form, 0)
        row = self._build_who(form, row)
        row = self._build_files(form, row)
        row = self._build_timing(form, row)
        self._build_notifications(form, row)

        self.summary = ttk.Label(self.root, padding=(12, 4), wraplength=800)
        self.summary.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.command_box = self.scrolledtext.ScrolledText(
            self.root, height=4, wrap="word", font=("Consolas", 10)
        )
        self.command_box.grid(row=3, column=0, columnspan=2, sticky="ew", padx=12)

        buttons = ttk.Frame(self.root, padding=(12, 6))
        buttons.grid(row=4, column=0, columnspan=2, sticky="ew")
        self.copy_button = ttk.Button(buttons, text="Copy command", command=self._copy)
        self.copy_button.grid(row=0, column=0, padx=(0, 6))
        self.run_button = ttk.Button(buttons, text="Run command", command=self._run)
        self.run_button.grid(row=0, column=1, padx=(0, 6))
        self.stop_button = ttk.Button(buttons, text="Stop", command=self._stop, state="disabled")
        self.stop_button.grid(row=0, column=2, padx=(0, 12))
        self.status = tk.Label(buttons, text="", anchor="w")
        self.status.grid(row=0, column=3, sticky="ew")
        buttons.columnconfigure(3, weight=1)

        self.output = self.scrolledtext.ScrolledText(self.root, height=10, wrap="word", font=("Consolas", 10))
        self.output.grid(row=5, column=0, columnspan=2, sticky="nsew", padx=12, pady=(0, 4))
        stdin_row = ttk.Frame(self.root, padding=(12, 0, 12, 10))
        stdin_row.grid(row=6, column=0, columnspan=2, sticky="ew")
        ttk.Label(stdin_row, text="Input").grid(row=0, column=0, padx=(0, 6))
        self.stdin_var = tk.StringVar()
        stdin = ttk.Entry(stdin_row, textvariable=self.stdin_var)
        stdin.grid(row=0, column=1, sticky="ew", padx=(0, 6))
        stdin.bind("<Return>", lambda _event: self._send_input())
        ttk.Button(stdin_row, text="Send to process", command=self._send_input).grid(row=0, column=2)
        stdin_row.columnconfigure(1, weight=1)
        ttk.Label(
            stdin_row,
            text="Type SEND here when a real send asks for it, or y when confirming one message.",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))

    def _build_function(self, parent, row: int) -> int:
        frame = self.ttk.LabelFrame(parent, text="Function", padding=8)
        frame.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        mode = self._var("mode", "send")
        for index, (value, label) in enumerate(
            (
                ("strip", "Strip export columns"),
                ("prepare", "Prepare dataset"),
                ("send", "Outreach (preview or send)"),
            )
        ):
            self.ttk.Radiobutton(frame, text=label, value=value, variable=mode).grid(
                row=0, column=index, sticky="w", padx=(0, 16)
            )
        return row + 1

    def _build_who(self, parent, row: int) -> int:
        frame = self.ttk.LabelFrame(parent, text="Who to contact", padding=8)
        frame.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        for index, (key, label) in enumerate(COUNTRY_FLAGS):
            variable = self.tk.BooleanVar(value=False)
            self.country_vars[key] = variable
            check = self.ttk.Checkbutton(frame, text=label, variable=variable)
            check.grid(row=0, column=index, sticky="w", padx=(0, 12))
            self._track(check, {"send"})
        hint = self.ttk.Label(
            frame,
            wraplength=760,
            text=(
                "Leave every box clear to contact all countries. Other is everyone outside "
                "Colombia, Mexico, Costa Rica, and Chile. Colombia and Costa Rica are usually "
                "addressed with usted; Mexico, Chile, and Other usually use tú. Put that wording "
                "in each country's message file."
            ),
        )
        hint.grid(row=1, column=0, columnspan=5, sticky="w", pady=(6, 6))
        self.date_mode = self._var("date_mode", "none")
        for index, (value, label) in enumerate(
            (("none", "No date filter"), ("on", "On this day"), ("range", "Date range"))
        ):
            button = self.ttk.Radiobutton(frame, text=label, value=value, variable=self.date_mode)
            button.grid(row=2, column=index, sticky="w", padx=(0, 12))
            self._track(button, {"send"})
        self._var("date", "")
        self._var("date_from", "")
        self._var("date_to", "")
        self.date_entry = self._inline_entry(frame, 3, 0, "Day", "date")
        self.date_from_entry = self._inline_entry(frame, 3, 1, "From", "date_from")
        self.date_to_entry = self._inline_entry(frame, 3, 2, "To", "date_to")
        return row + 1

    def _inline_entry(self, parent, row: int, column: int, label: str, name: str):
        holder = self.ttk.Frame(parent)
        holder.grid(row=row, column=column, sticky="w", pady=(6, 0))
        self.ttk.Label(holder, text=label).grid(row=0, column=0, padx=(0, 4))
        entry = self.ttk.Entry(holder, textvariable=self.vars[name], width=16)
        entry.grid(row=0, column=1)
        return entry

    def _build_files(self, parent, row: int) -> int:
        frame = self.ttk.LabelFrame(parent, text="Files and names", padding=8)
        frame.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        frame.columnconfigure(1, weight=1)
        fields = [
            ("input", "Input CSV", {"strip", "prepare", "send"}, [("CSV files", "*.csv"), ("All files", "*.*")]),
            ("strip_output", "Strip output", {"strip"}, [("CSV files", "*.csv"), ("All files", "*.*")]),
            ("cleaned_output", "Prepared output", {"prepare"}, [("CSV files", "*.csv"), ("All files", "*.*")]),
            ("messages", "Messages for Other / fallback", {"send"}, [("JSON files", "*.json"), ("All files", "*.*")]),
            ("messages_colombia", "Messages for Colombia", {"send"}, [("JSON files", "*.json"), ("All files", "*.*")]),
            ("messages_mexico", "Messages for Mexico", {"send"}, [("JSON files", "*.json"), ("All files", "*.*")]),
            ("messages_costa_rica", "Messages for Costa Rica", {"send"}, [("JSON files", "*.json"), ("All files", "*.*")]),
            ("messages_chile", "Messages for Chile", {"send"}, [("JSON files", "*.json"), ("All files", "*.*")]),
        ]
        line = 0
        for key, label, modes, filetypes in fields:
            self._var(key, str(DEFAULT_OPTIONS[key]))
            line = self._path_row(frame, line, key, label, modes, filetypes)
        self._var("campaign", "")
        self._var("academy_name", "")
        self._var("log", DEFAULT_OPTIONS["log"])
        self._var("review", DEFAULT_OPTIONS["review"])
        self._var("do_not_contact", DEFAULT_OPTIONS["do_not_contact"])
        self._var("default_region", DEFAULT_OPTIONS["default_region"])
        self._var("contact_order", DEFAULT_OPTIONS["contact_order"])
        line = self._entry_row(frame, line, "campaign", "Campaign", {"send"})
        line = self._entry_row(frame, line, "academy_name", "Academy name", {"send"})
        line = self._path_row(frame, line, "log", "Log", {"send"}, [("CSV files", "*.csv"), ("All files", "*.*")])
        line = self._path_row(
            frame, line, "review", "Review", {"prepare", "send"}, [("CSV files", "*.csv"), ("All files", "*.*")]
        )
        line = self._path_row(
            frame,
            line,
            "do_not_contact",
            "Do not contact",
            {"prepare", "send"},
            [("CSV files", "*.csv"), ("All files", "*.*")],
        )
        line = self._entry_row(frame, line, "default_region", "Default region", {"prepare", "send"})
        self.ttk.Label(frame, text="Contact order").grid(row=line, column=0, sticky="w", pady=2)
        order = self.ttk.Combobox(
            frame,
            textvariable=self.vars["contact_order"],
            values=("oldest-first", "file-order"),
            state="readonly",
            width=20,
        )
        order.grid(row=line, column=1, sticky="w", pady=2)
        self._track(order, {"send"}, "readonly")
        return row + 1

    def _build_timing(self, parent, row: int) -> int:
        frame = self.ttk.LabelFrame(parent, text="Timing and send flags", padding=8)
        frame.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        numbers = [
            ("max_messages", "Maximum messages", "10"),
            ("min_delay", "Minimum delay seconds", "45"),
            ("max_delay", "Maximum delay seconds", "90"),
            ("delay", "Fixed delay (blank = random)", ""),
            ("wait_time", "Wait time seconds", "20"),
            ("close_time", "Close time seconds", "3"),
            ("enter_presses", "Enter presses", "2"),
        ]
        for index, (key, label, default) in enumerate(numbers):
            self._var(key, default)
            grid_row, column = divmod(index, 2)
            self.ttk.Label(frame, text=label).grid(row=grid_row, column=column * 2, sticky="w", padx=(0, 6), pady=2)
            entry = self.ttk.Entry(frame, textvariable=self.vars[key], width=10)
            entry.grid(row=grid_row, column=column * 2 + 1, sticky="w", padx=(0, 16), pady=2)
            self._track(entry, {"send"})
        checks = self.ttk.Frame(frame)
        checks.grid(row=4, column=0, columnspan=4, sticky="w", pady=(6, 0))
        for index, (key, label) in enumerate(
            (
                ("send", "Send real WhatsApp messages (--send)"),
                ("yes", "Skip the SEND prompt (--yes)"),
                ("confirm_each", "Confirm each message"),
                ("click_composer", "Click the composer"),
            )
        ):
            self._bool_var(key, False)
            check = self.ttk.Checkbutton(checks, text=label, variable=self.vars[key])
            check.grid(row=index, column=0, sticky="w")
            self._track(check, {"send"})
            if key == "yes":
                self.yes_check = check
        return row + 1

    def _build_notifications(self, parent, row: int) -> int:
        frame = self.ttk.LabelFrame(parent, text="Notifications", padding=8)
        frame.grid(row=row, column=0, columnspan=3, sticky="ew")
        frame.columnconfigure(1, weight=1)
        self._var("ntfy_topic", "")
        self._var("ntfy_server", os.environ.get("NTFY_SERVER", "https://ntfy.sh"))
        self._var("ntfy_token", "")
        self._bool_var("include_ntfy_token", False)
        line = self._entry_row(frame, 0, "ntfy_topic", "ntfy topic", {"send"})
        line = self._entry_row(frame, line, "ntfy_server", "ntfy server", {"send"})
        line = self._entry_row(frame, line, "ntfy_token", "ntfy token", {"send"})
        include = self.ttk.Checkbutton(
            frame,
            text="Put the token in the copied command",
            variable=self.vars["include_ntfy_token"],
        )
        include.grid(row=line, column=1, sticky="w")
        self._track(include, {"send"})
        self.ttk.Label(
            frame,
            text="Otherwise a filled token is passed only to Run, through NTFY_TOKEN.",
        ).grid(row=line + 1, column=1, sticky="w")
        return row + 1

    def _entry_row(self, parent, row: int, key: str, label: str, modes: set[str]) -> int:
        self.ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2, padx=(0, 8))
        entry = self.ttk.Entry(parent, textvariable=self.vars[key])
        entry.grid(row=row, column=1, sticky="ew", pady=2)
        self._track(entry, modes)
        return row + 1

    def _path_row(self, parent, row: int, key: str, label: str, modes: set[str], filetypes) -> int:
        self.ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2, padx=(0, 8))
        entry = self.ttk.Entry(parent, textvariable=self.vars[key])
        entry.grid(row=row, column=1, sticky="ew", pady=2)
        button = self.ttk.Button(parent, text="Browse", command=lambda name=key, kinds=filetypes: self._browse(name, kinds))
        button.grid(row=row, column=2, padx=(6, 0))
        self._track(entry, modes)
        self._track(button, modes)
        return row + 1

    def _browse(self, key: str, filetypes) -> None:
        selected = self.filedialog.askopenfilename(
            initialdir=str(PROJECT_DIR),
            filetypes=filetypes,
        )
        if not selected:
            return
        path = Path(selected)
        try:
            shown = path.resolve().relative_to(PROJECT_DIR).as_posix()
        except ValueError:
            shown = str(path)
        self.vars[key].set(shown)

    def _wire_traces(self) -> None:
        for variable in self.vars.values():
            variable.trace_add("write", self._on_change)
        for variable in self.country_vars.values():
            variable.trace_add("write", self._on_change)

    def _on_change(self, *_args) -> None:
        if self._ready:
            self._refresh()

    def _options(self) -> dict:
        options = {name: variable.get() for name, variable in self.vars.items()}
        options["countries"] = [key for key, variable in self.country_vars.items() if variable.get()]
        return options

    def _apply_mode(self, mode: str, sending: bool) -> None:
        date_mode = self.date_mode.get()
        for widget, modes, enabled in self._controls:
            if mode not in modes:
                widget.configure(state="disabled")
            elif widget is getattr(self, "yes_check", None) and not sending:
                widget.configure(state="disabled")
            else:
                widget.configure(state=enabled)
        self.date_entry.configure(state="normal" if mode == "send" and date_mode == "on" else "disabled")
        range_on = mode == "send" and date_mode == "range"
        self.date_from_entry.configure(state="normal" if range_on else "disabled")
        self.date_to_entry.configure(state="normal" if range_on else "disabled")

    def _refresh(self) -> None:
        options = self._options()
        self._apply_mode(str(options["mode"]), bool(options.get("send")))
        try:
            self._args = build_outreach_args(options)
        except ValueError as exc:
            self._args = None
            self._set_box(self.command_box, "")
            self.summary.configure(text=describe_command(options))
            self.status.configure(text=str(exc), fg="#8a1f1f")
            self.copy_button.configure(state="disabled")
            if self._process is None:
                self.run_button.configure(state="disabled")
            return
        command = format_command(self._args)
        self._set_box(self.command_box, command)
        self.summary.configure(text=describe_command(options))
        self.status.configure(text="Command is ready.", fg="#1f4d2e")
        self.copy_button.configure(state="normal")
        if self._process is None:
            self.run_button.configure(state="normal")

    def _set_box(self, widget, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _copy(self) -> None:
        if not self._args:
            return
        command = format_command(self._args)
        self.root.clipboard_clear()
        self.root.clipboard_append(command)
        self.status.configure(text="Command copied.", fg="#1f4d2e")

    def _run(self) -> None:
        if not self._args or self._process is not None:
            return
        if "--send" in self._args and not self.messagebox.askokcancel(
            "Real send",
            "This will send real WhatsApp messages.\nContinue only after the list is reviewed.",
        ):
            return
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        token = str(self.vars["ntfy_token"].get()).strip()
        if token and not self.vars["include_ntfy_token"].get():
            env["NTFY_TOKEN"] = token
        self.output.configure(state="normal")
        self.output.insert("end", format_command(self._args) + "\n")
        self.output.see("end")
        self._process = subprocess.Popen(
            [project_python(), str(OUTREACH_SCRIPT), *self._args],
            cwd=PROJECT_DIR,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        self.run_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        threading.Thread(target=self._reader, daemon=True).start()
        self.root.after(80, self._poll)

    def _reader(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            self._queue.put(None)
            return
        for line in process.stdout:
            self._queue.put(line)
        self._queue.put(None)

    def _poll(self) -> None:
        finished = False
        try:
            while True:
                item = self._queue.get_nowait()
                if item is None:
                    finished = True
                    break
                self.output.insert("end", item)
                self.output.see("end")
        except queue.Empty:
            pass
        if finished and self._process is not None:
            code = self._process.wait()
            self.output.insert("end", f"\n[Process exited with code {code}]\n")
            self.output.see("end")
            self._process = None
            self.stop_button.configure(state="disabled")
            self._refresh()
            return
        if self._process is not None:
            self.root.after(80, self._poll)

    def _stop(self) -> None:
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()

    def _send_input(self) -> None:
        process = self._process
        text = self.stdin_var.get()
        if process is None or process.stdin is None or process.poll() is not None:
            self.status.configure(text="No command is waiting for input.", fg="#8a1f1f")
            return
        try:
            process.stdin.write(text + "\n")
            process.stdin.flush()
        except OSError as exc:
            self.output.insert("end", f"\n[Could not send input: {exc}]\n")
            return
        self.output.insert("end", f"> {text}\n")
        self.output.see("end")
        self.stdin_var.set("")

    def _on_close(self) -> None:
        self._stop()
        self.root.destroy()


if __name__ == "__main__":
    launch()
