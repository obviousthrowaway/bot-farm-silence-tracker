"""The CustomTkinter window. Thin: it draws what :mod:`.presenter` computes.

Only the Tk main thread touches widgets. A running scan reports through
:meth:`~bot_farm_silence_tracker.gui.presenter.ScanJob.drain`, which :meth:`App._poll`
calls on a timer.
"""

from __future__ import annotations

import contextlib
import sys
import tempfile
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Any

import customtkinter as ctk

from bot_farm_silence_tracker import config as user_config
from bot_farm_silence_tracker.gui import presenter as P

TITLE = "Bot Farm Silence Tracker"
MIN_WIDTH, MIN_HEIGHT = 900, 650
SCREENS = (
    ("platforms", "Platforms"),
    ("scan", "New scan"),
    ("run", "Run"),
    ("results", "Results"),
    ("network", "Network and privacy"),
)
POLL_MS = 100
WRAP = 620
ERROR_COLOR = ("#b00020", "#ff8a80")
MUTED = ("#555555", "#aaaaaa")
FOCUS_COLOR = "#1f6aa5"
MOD = "Command" if sys.platform == "darwin" else "Control"


def _dark() -> bool:
    return ctk.get_appearance_mode().lower() == "dark"


def _colors() -> dict[str, str]:
    if _dark():
        return {
            "bg": "#242424",
            "fg": "#e8e8e8",
            "muted": "#9a9a9a",
            "line": "#555555",
            "bar": "#6ea8d8",
            "cut": "#ff6b6b",
            "marker": "#c39bd3",
            "node": "#8a8a8a",
            "sel": "#1f538d",
        }
    return {
        "bg": "#ffffff",
        "fg": "#1a1a1a",
        "muted": "#666666",
        "line": "#bbbbbb",
        "bar": "#3a7ebf",
        "cut": "#c62828",
        "marker": "#7b3fa0",
        "node": "#777777",
        "sel": "#cfe3f7",
    }


def keyboard(widget: Any, activate: Callable[[], Any] | None = None) -> Any:
    """Let ``widget`` take keyboard focus, show it, and run ``activate`` on Space/Return.

    CustomTkinter's buttons, check boxes, switches and menus are drawn on canvases
    and are not focusable by default.
    """
    tk.Frame.configure(widget, takefocus=1, highlightthickness=2)

    def show(focused: bool) -> None:
        try:
            bg = tk.Frame.cget(widget, "bg")
            tk.Frame.configure(
                widget, highlightbackground=bg, highlightcolor=FOCUS_COLOR if focused else bg
            )
        except tk.TclError:
            pass

    show(False)
    widget.bind("<FocusIn>", lambda _e: show(True), add="+")
    widget.bind("<FocusOut>", lambda _e: show(False), add="+")
    if activate is not None:
        widget.bind("<space>", lambda _e: activate(), add="+")
        widget.bind("<Return>", lambda _e: activate(), add="+")
    return widget


def option_menu(
    master: Any, values: list[str], command: Callable[[str], Any] | None = None, **kwargs: Any
) -> ctk.CTkOptionMenu:
    """An option menu that Up/Down change and Space opens."""
    menu = ctk.CTkOptionMenu(master, values=values or [""], command=command, **kwargs)

    def step(delta: int) -> None:
        options = list(menu.cget("values"))
        if not options:
            return
        current = menu.get()
        index = options.index(current) if current in options else 0
        choice = options[(index + delta) % len(options)]
        menu.set(choice)
        if command is not None:
            command(choice)

    keyboard(menu, getattr(menu, "_open_dropdown_menu", None))
    menu.bind("<Up>", lambda _e: step(-1), add="+")
    menu.bind("<Down>", lambda _e: step(1), add="+")
    return menu


def button(master: Any, text: str, command: Callable[[], Any], **kwargs: Any) -> ctk.CTkButton:
    return keyboard(ctk.CTkButton(master, text=text, command=command, **kwargs), command)


def heading(master: Any, text: str) -> ctk.CTkLabel:
    return ctk.CTkLabel(master, text=text, font=ctk.CTkFont(size=20, weight="bold"), anchor="w")


def note(master: Any, text: str, **kwargs: Any) -> ctk.CTkLabel:
    kwargs.setdefault("text_color", MUTED)
    kwargs.setdefault("wraplength", WRAP)
    return ctk.CTkLabel(master, text=text, justify="left", anchor="w", **kwargs)


def set_text(box: ctk.CTkTextbox, text: str) -> None:
    box.configure(state="normal")
    box.delete("1.0", "end")
    box.insert("end", text)
    box.configure(state="disabled")


#: Set once the process has created its window (see :class:`App`).
_ROOT_CREATED = False
#: Why a second window is refused.
ONE_WINDOW = (
    "The desktop app opens one window per process. Creating a second Tk root window "
    "after the first (even a closed one) can crash Tk 9 on macOS, so it is refused."
)


class App(ctk.CTk):
    """The main window: a sidebar of screens and the current screen.

    Only one may be created per process: CustomTkinter widgets call ``update()``
    while they are built, and under Tk 9.0 on macOS that crashes (a segmentation
    fault in ``showRootWindow``) once an earlier root window has been destroyed.
    A second ``App`` raises :class:`RuntimeError` with :data:`ONE_WINDOW` instead.

    With ``hidden`` the window is withdrawn before any widget is built, so the
    self-test and the tests never show it on the desktop.
    """

    def __init__(self, model: P.AppState | None = None, *, hidden: bool = False) -> None:
        global _ROOT_CREATED
        if _ROOT_CREATED:
            raise RuntimeError(ONE_WINDOW)
        _ROOT_CREATED = True
        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")
        super().__init__()
        if hidden:
            self.withdraw()
        self.model = model or P.AppState()
        self.title(TITLE)
        self.geometry("1120x760")
        self.minsize(MIN_WIDTH, MIN_HEIGHT)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        side = ctk.CTkFrame(self, corner_radius=0, width=200)
        side.grid(row=0, column=0, sticky="nsw")
        ctk.CTkLabel(
            side,
            text=TITLE,
            font=ctk.CTkFont(size=15, weight="bold"),
            wraplength=170,
            justify="left",
        ).pack(padx=16, pady=(18, 12), anchor="w")
        self.nav: dict[str, ctk.CTkButton] = {}
        for index, (key, label) in enumerate(SCREENS, 1):
            b = button(
                side,
                label,
                lambda k=key: self.show(k),
                anchor="w",
                fg_color="transparent",
                text_color=("gray10", "gray90"),
                hover_color=("gray75", "gray30"),
            )
            b.pack(fill="x", padx=10, pady=3)
            self.nav[key] = b
            self.bind(f"<{MOD}-Key-{index}>", lambda _e, k=key: self.show(k))
        self.hide_var = tk.BooleanVar(value=self.model.hide_handles)
        hide = ctk.CTkSwitch(
            side, text="Hide account names", variable=self.hide_var, command=self._hide_changed
        )
        keyboard(hide, hide.toggle)
        hide.pack(side="bottom", padx=14, pady=(6, 16), anchor="w")
        note(
            side, f"Keys: {MOD}+1 to 5 switch screens; Tab moves between controls.", wraplength=170
        ).pack(side="bottom", padx=14, anchor="w")

        self.content = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.content.grid(row=0, column=1, sticky="nsew")
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)
        self.screens: dict[str, Any] = {
            "platforms": PlatformsScreen(self.content, self),
            "scan": ScanScreen(self.content, self),
            "run": RunScreen(self.content, self),
            "results": ResultsScreen(self.content, self),
            "network": NetworkScreen(self.content, self),
        }
        for screen in self.screens.values():
            screen.grid(row=0, column=0, sticky="nsew")
        self.current = ""
        self.show("platforms")
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _e: self.screens["run"].cancel())
        self._poll_id: str | None = self.after(POLL_MS, self._poll)

    def show(self, key: str) -> None:
        self.current = key
        screen = self.screens[key]
        screen.tkraise()
        screen.on_show()
        for name, b in self.nav.items():
            b.configure(fg_color=("gray75", "gray25") if name == key else "transparent")

    def _hide_changed(self) -> None:
        self.model.hide_handles = bool(self.hide_var.get())
        self.screens["results"].reload()

    def start_job(self, job: P.ScanJob) -> None:
        if self.model.job is not None and self.model.job.running:
            return
        self.model.job = job
        self.screens["run"].started(job)
        job.start()
        self.show("run")

    def _poll(self) -> None:
        job = self.model.job
        if job is not None:
            events = job.drain()
            if events:
                self.screens["run"].handle(events)
            self.screens["run"].tick()
        self._poll_id = self.after(POLL_MS, self._poll)

    def forget_session(self) -> None:
        """Stop a running scan cleanly and forget the session credentials."""
        job = self.model.job
        if job is not None and job.running:
            job.cancel()
            job.join(timeout=10)
        self.model.credentials.clear()
        self.screens["scan"].clear_credentials()

    def close(self) -> None:
        """Forget the session (see :meth:`forget_session`) and close the window."""
        self.forget_session()
        if self._poll_id is not None:
            self.after_cancel(self._poll_id)
            self._poll_id = None
        self.destroy()


class Screen(ctk.CTkFrame):
    def __init__(self, master: Any, app: App) -> None:
        super().__init__(master, corner_radius=0, fg_color="transparent")
        self.app = app

    def on_show(self) -> None:
        pass


class PlatformsScreen(Screen):
    def __init__(self, master: Any, app: App) -> None:
        super().__init__(master, app)
        heading(self, "Platforms").pack(padx=24, pady=(20, 4), anchor="w")
        note(
            self,
            "Choose the platforms you plan to scan and the one New scan starts "
            "with. This is the same setting as `bot-farm-silence-tracker config "
            "set-platforms`. You can still scan any platform.",
        ).pack(padx=24, anchor="w")
        box = ctk.CTkFrame(self)
        box.pack(padx=24, pady=12, fill="x")
        self.vars: dict[str, tk.BooleanVar] = {}
        for name in P.platform_names():
            row = ctk.CTkFrame(box, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=4)
            var = tk.BooleanVar()
            check = ctk.CTkCheckBox(row, text=name, variable=var, width=170, command=self._changed)
            keyboard(check, check.toggle)
            check.pack(side="left")
            note(row, P.PLATFORM_BLURBS.get(name, ""), wraplength=480).pack(side="left")
            self.vars[name] = var
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(padx=24, pady=4, fill="x")
        ctk.CTkLabel(row, text="Default platform:").pack(side="left")
        self.default = option_menu(row, [user_config.DEFAULT_PLATFORM])
        self.default.pack(side="left", padx=8)
        button(row, "Save", self.save).pack(side="left", padx=8)
        self.status = note(self, "")
        self.status.pack(padx=24, pady=8, anchor="w")
        self.load()

    def load(self) -> None:
        settings = P.load_platform_settings()
        for name, var in self.vars.items():
            var.set(name in settings.enabled)
        self._changed(settings.default)
        lines = [f"Config file: {settings.path}"]
        lines += list(settings.warnings)
        if settings.error:
            lines.append(f"The config file cannot be used ({settings.error}); saving replaces it.")
        self.status.configure(text="\n".join(lines), text_color=MUTED)

    def chosen(self) -> list[str]:
        return [name for name, var in self.vars.items() if var.get()]

    def _changed(self, default: str | None = None) -> None:
        names = self.chosen() or [user_config.DEFAULT_PLATFORM]
        current = default or self.default.get()
        self.default.configure(values=names)
        self.default.set(current if current in names else names[0])

    def save(self) -> None:
        try:
            saved = P.save_platform_settings(self.chosen(), self.default.get())
        except user_config.ConfigError as exc:
            self.status.configure(text=f"Not saved: {exc}.", text_color=ERROR_COLOR)
            return
        self.status.configure(
            text=f"Saved to {saved.path}: {', '.join(saved.enabled)} (default {saved.default}).",
            text_color=MUTED,
        )
        self.app.screens["scan"].set_platform(saved.default)


class ScanScreen(Screen):
    def __init__(self, master: Any, app: App) -> None:
        super().__init__(master, app)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        heading(self, "New scan").grid(row=0, column=0, padx=24, pady=(20, 4), sticky="w")
        body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=12)
        body.grid_columnconfigure(1, weight=1)
        self.body = body
        r = 0

        def label(text: str) -> None:
            ctk.CTkLabel(body, text=text, anchor="w").grid(
                row=r, column=0, sticky="nw", padx=12, pady=6
            )

        label("Platform")
        self.platform = option_menu(body, P.platform_names(), command=self.set_platform)
        self.platform.grid(row=r, column=1, sticky="w", pady=6)
        r += 1
        label("Sample size (accounts)")
        self.n = ctk.CTkEntry(body, width=120)
        self.n.insert(0, str(P.DEFAULT_SAMPLE))
        self.n.grid(row=r, column=1, sticky="w", pady=6)
        self.n.bind("<KeyRelease>", lambda _e: self.update_estimate())
        r += 1
        self.estimate = note(body, "", wraplength=440)
        self.estimate.grid(row=r, column=1, sticky="w")
        r += 1
        label("Outage event")
        self.event = option_menu(
            body, [e.label for e in P.event_choices()], command=self._event_chosen, width=300
        )
        # The default event may be a custom date (config set-default-event custom-...).
        self.event.set(P.event_for_cutoff(P.default_cutoff()).label)
        self.event.grid(row=r, column=1, sticky="w", pady=6)
        r += 1
        label("Cutoff (UTC)")
        self.picker = CutoffPicker(body, on_change=self._cutoff_changed)
        self.picker.grid(row=r, column=1, sticky="w", pady=6)
        r += 1
        note(
            body,
            "When the reported outage began. Accounts that posted steadily "
            "before it and went silent right at it are flagged for review. Choose an outage event "
            "(each keeps its own results), or pick any date and time for a custom event "
            "that stays on this computer.",
            wraplength=440,
        ).grid(row=r, column=1, sticky="w")
        r += 1
        self.seed_label = ctk.CTkLabel(body, text="Seed communities", anchor="w")
        self.seed_label.grid(row=r, column=0, sticky="nw", padx=12, pady=6)
        self.seeds = ctk.CTkTextbox(body, height=70, wrap="word")
        self.seeds.grid(row=r, column=1, sticky="ew", pady=6, padx=(0, 12))
        r += 1
        self.seed_note = note(body, "", wraplength=440)
        self.seed_note.grid(row=r, column=1, sticky="w")
        r += 1
        self.x_frame = ctk.CTkFrame(body)
        self.x_frame.grid(row=r, column=0, columnspan=2, sticky="ew", padx=12, pady=6)
        ctk.CTkLabel(self.x_frame, text="Spend cap in US dollars (X only)").grid(
            row=0, column=0, padx=12, pady=6, sticky="w"
        )
        self.max_usd = ctk.CTkEntry(self.x_frame, width=100)
        self.max_usd.grid(row=0, column=1, sticky="w")
        self.confirm_var = tk.BooleanVar()
        confirm = ctk.CTkCheckBox(
            self.x_frame,
            text="I understand X bills for discovery and fetching, up to the cap",
            variable=self.confirm_var,
        )
        keyboard(confirm, confirm.toggle)
        confirm.grid(row=1, column=0, columnspan=2, padx=12, pady=6, sticky="w")
        r += 1
        self.cred_frame = ctk.CTkFrame(body)
        self.cred_frame.grid(row=r, column=0, columnspan=2, sticky="ew", padx=12, pady=6)
        self.cred_frame.grid_columnconfigure(1, weight=1)
        self.cred_entries: dict[str, ctk.CTkEntry] = {}
        r += 1
        self.adv_button = button(
            body,
            "Show advanced options",
            self.toggle_advanced,
            fg_color="transparent",
            border_width=1,
            text_color=("gray10", "gray90"),
        )
        self.adv_button.grid(row=r, column=0, sticky="w", padx=12, pady=6)
        r += 1
        self.adv = ctk.CTkFrame(body)
        self.adv_row = r
        self.concurrency = self._adv_entry(0, "Accounts fetched at once", "1")
        self.max_requests = self._adv_entry(1, "Request limit (empty: none)", "")
        self.max_errors = self._adv_entry(
            2, "Errors in a row before stopping", str(P.DEFAULT_MAX_CONSECUTIVE_ERRORS)
        )
        self.adv_note = note(self.adv, "", wraplength=520)
        self.adv_note.grid(row=3, column=0, columnspan=2, padx=12, pady=(0, 8), sticky="w")
        r += 1
        self.awake_var = tk.BooleanVar(value=P.load_keep_awake())
        awake = ctk.CTkCheckBox(
            body,
            text="Keep this computer awake while scanning",
            variable=self.awake_var,
            command=self._awake_changed,
        )
        keyboard(awake, awake.toggle)
        awake.grid(row=r, column=0, columnspan=2, sticky="w", padx=12, pady=6)
        r += 1
        out = ctk.CTkFrame(body, fg_color="transparent")
        out.grid(row=r, column=0, columnspan=2, sticky="ew", padx=12, pady=6)
        ctk.CTkLabel(out, text="Results folder:").pack(side="left")
        self.out_label = ctk.CTkLabel(out, text=str(app.model.output_dir))
        self.out_label.pack(side="left", padx=8)
        button(out, "Change...", self.choose_folder, width=90).pack(side="left")
        r += 1
        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.grid(row=2, column=0, sticky="ew", padx=24, pady=12)
        self.errors = ctk.CTkLabel(
            foot, text="", text_color=ERROR_COLOR, wraplength=WRAP, justify="left", anchor="w"
        )
        self.errors.pack(side="left", fill="x", expand=True)
        self.start_button = button(foot, "Start scan", self.start)
        self.start_button.pack(side="right")
        self.app.bind(f"<{MOD}-Return>", lambda _e: self._start_if_shown(), add="+")
        self.set_platform(P.load_platform_settings().default)

    def _awake_changed(self) -> None:
        try:
            P.save_keep_awake(bool(self.awake_var.get()))
            self.errors.configure(text="")
        except user_config.ConfigError as exc:
            self.errors.configure(text=f"Could not save this choice: {exc}")

    def _adv_entry(self, row: int, text: str, value: str) -> ctk.CTkEntry:
        ctk.CTkLabel(self.adv, text=text).grid(row=row, column=0, padx=12, pady=6, sticky="w")
        entry = ctk.CTkEntry(self.adv, width=100)
        entry.insert(0, value)
        entry.grid(row=row, column=1, sticky="w", pady=6)
        return entry

    def toggle_advanced(self) -> None:
        if self.adv.winfo_ismapped():
            self.adv.grid_remove()
            self.adv_button.configure(text="Show advanced options")
        else:
            self.adv.grid(row=self.adv_row, column=0, columnspan=2, sticky="ew", padx=12, pady=6)
            self.adv_button.configure(text="Hide advanced options")

    def set_platform(self, platform: str) -> None:
        self.platform.set(platform)
        kind = P.seed_kind(platform)
        defaults = P.default_seeds(platform)
        self.seed_label.configure(text=f"Seed {kind}")
        shown = ", ".join(defaults[:12]) + (", ..." if len(defaults) > 12 else "")
        self.seed_note.configure(
            text=f"One per line. Leave empty to use the defaults: {shown or 'none'}."
        )
        if platform == "x":
            self.x_frame.grid()
        else:
            self.x_frame.grid_remove()
        top = P.max_concurrency(platform)
        self.adv_note.configure(
            text=(
                f"{platform} fetches up to {top} accounts at once. The request rate stays "
                "capped by the platform's limit whatever this says."
                if top > 1
                else f"{platform} fetches one account at a time."
            )
        )
        self._build_credentials(platform)
        self.update_estimate()

    def _build_credentials(self, platform: str) -> None:
        for child in self.cred_frame.winfo_children():
            child.destroy()
        self.cred_entries = {}
        fields = P.credential_fields(platform)
        if not fields:
            note(self.cred_frame, f"{platform} needs no login or API key.").grid(
                row=0, column=0, columnspan=2, padx=12, pady=8, sticky="w"
            )
            return
        note(
            self.cred_frame,
            "Credentials. Setting the environment variables is the "
            "usual way. You can instead type them here: they are kept in memory for this "
            "session only, never saved, and forgotten when the app closes.",
            wraplength=640,
        ).grid(row=0, column=0, columnspan=2, padx=12, pady=(8, 4), sticky="w")
        row = 1
        for f in fields:
            need = "required" if f.required else "optional"
            ctk.CTkLabel(self.cred_frame, text=f"{f.label} ({need})").grid(
                row=row, column=0, padx=12, pady=(6, 0), sticky="w"
            )
            entry = ctk.CTkEntry(
                self.cred_frame,
                show="•" if f.secret else "",
                placeholder_text="set in the environment" if f.from_environment else "",
            )
            entry.grid(row=row, column=1, sticky="ew", padx=(0, 12), pady=(6, 0))
            # Typing, pasting (also with the middle mouse button) and a password manager
            # all change the text; leaving the field catches what no key event reports.
            # start() and resume() read the fields again, so none is ever missed.
            for sequence in ("<KeyRelease>", "<<Paste>>", "<ButtonRelease-2>", "<FocusOut>"):
                entry.bind(sequence, lambda _e, env=f.env, w=entry: self._remember(env, w))
            # Fields are rebuilt when the platform changes: show what is already set for
            # this session instead of leaving a value that is set but invisible.
            kept = self.app.model.credentials.snapshot().get(f.env)
            if kept:
                entry.insert(0, kept)
            self.cred_entries[f.env] = entry
            row += 1
            hosts = "; ".join(f"{host} ({how})" for host, how in f.receivers) or "no host"
            note(self.cred_frame, f"{f.env}. Sent only to: {hosts}.", wraplength=640).grid(
                row=row, column=0, columnspan=2, padx=12, sticky="w"
            )
            row += 1

    def _remember(self, env: str, entry: Any) -> None:
        with contextlib.suppress(tk.TclError):
            self.app.model.credentials.set(env, entry.get())

    def sync_credentials(self) -> None:
        """Take whatever the fields hold now into the session credentials."""
        for env, entry in self.cred_entries.items():
            self._remember(env, entry)

    def clear_credentials(self) -> None:
        for entry in self.cred_entries.values():
            with contextlib.suppress(tk.TclError):
                entry.delete(0, "end")

    def choose_folder(self) -> None:
        chosen = filedialog.askdirectory(
            initialdir=str(self.app.model.output_dir), title="Folder for scan results"
        )
        if chosen:
            self.app.model.output_dir = Path(chosen)
            self.out_label.configure(text=chosen)

    def _event_chosen(self, label: str) -> None:
        event = P.event_by_label(label)
        if event.cutoff is not None:
            self.picker.set(event.cutoff)
        self._follow_event()

    def _cutoff_changed(self) -> None:
        try:
            cutoff = self.picker.get()
        except ValueError:
            return
        self.event.set(P.event_for_cutoff(cutoff).label)
        self._follow_event()

    def _follow_event(self) -> None:
        """Point the Results and Resume screens at the chosen event's store."""
        try:
            chosen = P.event_of_choice(P.event_by_label(self.event.get()).id, self.picker.get())
        except ValueError:
            return
        self.app.model.event_id = chosen.id

    def form(self) -> P.ScanForm:
        """The form; raises ``ValueError`` when the picked date does not exist."""
        cutoff = self.picker.get()
        return P.ScanForm(
            platform=self.platform.get(),
            n=self.n.get(),
            event=P.event_by_label(self.event.get()).id,
            cutoff=cutoff,
            seeds=self.seeds.get("1.0", "end"),
            concurrency=self.concurrency.get(),
            max_requests=self.max_requests.get(),
            max_consecutive_errors=self.max_errors.get(),
            max_usd=self.max_usd.get(),
            confirm_billed=bool(self.confirm_var.get()),
            keep_awake=bool(self.awake_var.get()),
        )

    def update_estimate(self) -> None:
        try:
            n = int(self.n.get().strip())
        except ValueError:
            self.estimate.configure(text="")
            return
        if n >= 1:
            self.estimate.configure(text=P.estimate_text(self.platform.get(), n))

    def _start_if_shown(self) -> None:
        if self.app.current == "scan":
            self.start()

    def start(self) -> None:
        try:
            form = self.form()
        except ValueError as exc:
            self.errors.configure(text=str(exc))
            return
        request, problems = P.validate_form(form)
        if request is None:
            self.errors.configure(text="\n".join(problems))
            return
        self.errors.configure(text="")
        model = self.app.model
        argv = P.scan_argv(
            request,
            out=P.output_path(model.output_dir),
            db=model.db,
            hide_handles=model.hide_handles,
        )
        model.output_dir.mkdir(parents=True, exist_ok=True)
        self.sync_credentials()
        model.event_id = request.event_id
        self.app.start_job(
            P.ScanJob(argv, model.credentials.snapshot(), db=model.db, event_id=request.event_id)
        )


class CutoffPicker(ctk.CTkFrame):
    """A UTC date and time picker: year, month, day, hour and minute menus, with a
    line below that shows the same moment in the computer's local time zone."""

    def __init__(self, master: Any, on_change: Callable[[], Any] | None = None) -> None:
        super().__init__(master, fg_color="transparent")
        self._on_change = on_change
        choices = P.picker_choices()
        widths = {"year": 80, "month": 90, "day": 64, "hour": 64, "minute": 64}
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(side="top", anchor="w")
        self.menus: dict[str, ctk.CTkOptionMenu] = {}
        for key in ("year", "month", "day", "hour", "minute"):
            if key == "hour":
                ctk.CTkLabel(row, text="at").pack(side="left", padx=(8, 4))
            if key == "minute":
                ctk.CTkLabel(row, text=":").pack(side="left", padx=2)
            menu = option_menu(
                row, choices[key], command=lambda _v: self._changed(), width=widths[key]
            )
            menu.pack(side="left", padx=2)
            self.menus[key] = menu
        self.local = note(self, "")
        self.local.pack(side="top", anchor="w", padx=2)
        self.set(P.DEFAULT_CUTOFF)

    def _show_local(self) -> None:
        """Show the picked UTC moment in the computer's local time (a hint only)."""
        try:
            text = P.local_picker_line(self.get())
        except ValueError:
            text = ""
        self.local.configure(text=text)

    def _changed(self) -> None:
        self._show_local()
        if self._on_change is not None:
            self._on_change()

    def set(self, cutoff: Any) -> None:
        for key, value in P.picker_values(cutoff).items():
            menu = self.menus[key]
            values = list(menu.cget("values"))
            if value not in values:  # A year outside the menu's range.
                menu.configure(values=sorted([*values, value], reverse=key == "year"))
            menu.set(value)
        self._show_local()

    def get(self) -> Any:
        """The picked cutoff; raises ``ValueError`` for a date that does not exist."""
        return P.cutoff_from_picker({key: menu.get() for key, menu in self.menus.items()})


class RunScreen(Screen):
    def __init__(self, master: Any, app: App) -> None:
        super().__init__(master, app)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)
        heading(self, "Run").grid(row=0, column=0, padx=24, pady=(20, 4), sticky="w")
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=1, column=0, padx=24, sticky="w")
        self.status = note(
            top,
            "No scan is running. Start one under New scan, or resume an interrupted run below.",
            text_color=("gray10", "gray90"),
        )
        self.status.pack(anchor="w")
        self.stall = note(top, "", text_color=ERROR_COLOR, wraplength=WRAP)
        self.stall.pack(anchor="w")
        self.bar = ctk.CTkProgressBar(self)
        self.bar.set(0)
        self.bar.grid(row=2, column=0, padx=24, pady=10, sticky="ew")
        self.log = ctk.CTkTextbox(self, wrap="none", font=ctk.CTkFont(family="Courier", size=12))
        self.log.configure(state="disabled")
        self.log.grid(row=3, column=0, padx=24, sticky="nsew")
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.grid(row=4, column=0, padx=24, pady=10, sticky="ew")
        self.cancel_button = button(
            row, "Cancel (Esc)", self.cancel, state="disabled", fg_color=("#b00020", "#8e1c1c")
        )
        self.cancel_button.pack(side="left")
        self.results_button = button(row, "Show results", self.show_results, state="disabled")
        self.results_button.pack(side="left", padx=8)
        button(row, "Open results folder", lambda: P.open_folder(app.model.output_dir)).pack(
            side="left"
        )
        resume = ctk.CTkFrame(self)
        resume.grid(row=5, column=0, padx=24, pady=(0, 16), sticky="ew")
        ctk.CTkLabel(resume, text="Resume an interrupted run:").pack(side="left", padx=12, pady=8)
        self.resume_menu = option_menu(resume, ["(none)"], width=360)
        self.resume_menu.pack(side="left", padx=4)
        self.resume_button = button(resume, "Resume", self.resume)
        self.resume_button.pack(side="left", padx=8)
        self.runs: dict[str, dict[str, Any]] = {}
        self.pending: list[str] = []

    def on_show(self) -> None:
        self.refresh_runs()

    def refresh_runs(self) -> None:
        try:
            runs = P.resumable_runs(self.app.model.db, self.app.model.event_id)
        except Exception as exc:  # An unreadable database: say so, do not crash.
            runs = []
            self.status.configure(text=f"Could not read the database: {exc}")
        self.runs = {P.run_label(r): r for r in runs}
        labels = list(self.runs) or ["(none)"]
        self.resume_menu.configure(values=labels)
        self.resume_menu.set(labels[0])

    def resume(self) -> None:
        run = self.runs.get(self.resume_menu.get())
        if run is None:
            return
        if P.run_in_use(run):  # Checked again: the list may be older than the scan.
            self.status.configure(text=P.IN_USE_TEXT)
            return
        model = self.app.model
        # The store the run was listed from, even if the event menu changed since.
        store = Path(run.get("store") or P.event_store(model.event_id, model.db))
        argv = P.resume_argv(
            str(run["platform"]),
            int(run["run_id"]),
            out=P.output_path(model.output_dir),
            db=store,
            hide_handles=model.hide_handles,
            event_id=run.get("event_id"),
            keep_awake=P.load_keep_awake(),
        )
        model.output_dir.mkdir(parents=True, exist_ok=True)
        self.app.screens["scan"].sync_credentials()
        self.app.start_job(
            P.ScanJob(
                argv,
                model.credentials.snapshot(),
                db=store,
                event_id=run.get("store_event")
                or (None if model.db is not None else P.default_event().id),
            )
        )

    def started(self, job: P.ScanJob) -> None:
        set_text(self.log, "")
        self.bar.configure(mode="indeterminate")
        self.bar.start()
        self.status.configure(
            text="Scanning. Finding accounts first; the bar fills once fetching starts."
        )
        self.cancel_button.configure(state="normal")
        self.results_button.configure(state="disabled")
        self.resume_button.configure(state="disabled")

    def tick(self) -> None:
        """Show or clear the stall hint (called by the app's poll while a scan runs)."""
        job = self.app.model.job
        hint = job.stall_hint() if job is not None else None
        if hint is None:
            self.stall.configure(text="")
        else:
            self.stall.configure(text=f"{hint} {P.last_request_text(job.last_request_at)}")

    def cancel(self) -> None:
        job = self.app.model.job
        if job is not None and job.running and not job.cancel_requested:
            job.cancel()
            self.status.configure(
                text="Cancelling: finishing the current account and saving the run..."
            )
            self.cancel_button.configure(state="disabled")

    def handle(self, events: list[P.Event]) -> None:
        lines: list[str] = []
        for event in events:
            if isinstance(event, P.LogLine):
                lines.append(event.text)
            elif isinstance(event, P.Progress):
                if self.bar.cget("mode") != "determinate":
                    self.bar.stop()
                    self.bar.configure(mode="determinate")
                self.bar.set(event.done / max(1, event.total))
                if not self.app.model.job or not self.app.model.job.cancel_requested:
                    self.status.configure(text=f"Fetching account {event.done} of {event.total}.")
            elif isinstance(event, P.Finished):
                self.finished(event)
        if lines:
            self.log.configure(state="normal")
            self.log.insert("end", "\n".join(lines) + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")

    def finished(self, event: P.Finished) -> None:
        self.bar.stop()
        self.stall.configure(text="")
        self.bar.configure(mode="determinate")
        if event.run_status == "complete":
            self.bar.set(1)
        self.status.configure(text=event.message)
        self.cancel_button.configure(state="disabled")
        self.resume_button.configure(state="normal")
        if event.run_id is not None:
            # The run is in the store of the scan's event, whatever the menu shows now.
            if event.event_id is not None:
                self.app.model.event_id = event.event_id
            self.app.model.last_run_id = event.run_id
            self.results_button.configure(state="normal")
        self.refresh_runs()

    def show_results(self) -> None:
        self.app.screens["results"].select_run(self.app.model.last_run_id)
        self.app.show("results")


class ResultsScreen(Screen):
    COLUMNS = (
        ("rank", "#", 36),
        ("account", "Account", 150),
        ("signals", "Signals", 80),
        ("score", "Score", 55),
        ("cluster", "Stop group", 72),
        ("last", "Last post (UTC)", 125),
        ("marker", "Nearest marker", 170),
        ("ppd", "Posts/day", 66),
        ("communities", "Seen in", 120),
        ("also", "Also in event", 110),
    )

    def __init__(self, master: Any, app: App) -> None:
        super().__init__(master, app)
        self.results: P.Results | None = None
        self.filters = P.Filters()
        self.shown: list[P.FlaggedAccount] = []
        self.run_ids: dict[str, int] = {}
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)
        heading(self, "Results").grid(row=0, column=0, padx=24, pady=(20, 2), sticky="w")
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=1, column=0, padx=24, sticky="ew")
        ctk.CTkLabel(top, text="Run:").pack(side="left")
        self.run_menu = option_menu(top, ["(no runs)"], command=lambda _v: self.reload(), width=380)
        self.run_menu.pack(side="left", padx=6)
        button(top, "Refresh", self.refresh_runs, width=80).pack(side="left")
        self.message = note(self, P.NOT_PROOF)
        self.message.grid(row=2, column=0, padx=24, pady=4, sticky="w")
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=3, column=0, padx=24, sticky="ew")
        self.filter_menus: dict[str, ctk.CTkOptionMenu] = {}
        filters = (
            ("platform", "Platform"),
            ("community", "Community"),
            ("cluster", "Stop group"),
            ("signals", "Signals"),
            ("marker", "Nearest marker"),
        )
        for index, (key, text) in enumerate(filters):
            row, col = divmod(index, 2)
            ctk.CTkLabel(bar, text=f"{text}:").grid(
                row=row, column=col * 2, sticky="w", padx=(0, 4), pady=2
            )
            menu = option_menu(bar, [P.ALL], command=lambda _v: self.apply_filters(), width=200)
            menu.grid(row=row, column=col * 2 + 1, sticky="w", padx=(0, 16), pady=2)
            self.filter_menus[key] = menu
        self.tabs = ctk.CTkTabview(self)
        self.tabs.grid(row=4, column=0, padx=20, pady=4, sticky="nsew")
        self._build_table(self.tabs.add("Flagged accounts"))
        self._build_clusters(self.tabs.add("Stop groups"))
        self.graph = tk.Canvas(self.tabs.add("Account ↔ community map"), highlightthickness=0)
        self.graph.pack(fill="both", expand=True)
        self.graph.bind("<Configure>", lambda _e: self.draw_graph())
        self._build_timeline(self.tabs.add("Silence timeline"))
        self.summary = ctk.CTkTextbox(self.tabs.add("Summary"), wrap="word")
        self.summary.pack(fill="both", expand=True)
        self.details = ctk.CTkTextbox(self.tabs.add("Outage event"), wrap="word")
        self.details.pack(fill="both", expand=True)
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=5, column=0, padx=24, pady=(4, 14), sticky="ew")
        self.profile_button = button(actions, "Open profile", self.open_profile)
        self.profile_button.pack(side="left")
        self.community_button = button(actions, "Open community page", self.open_community)
        self.community_button.pack(side="left", padx=8)
        button(actions, "Open results folder", lambda: P.open_folder(app.model.output_dir)).pack(
            side="left"
        )
        self.export_button = button(actions, "Export CSV...", self.export)
        self.export_button.pack(side="left", padx=8)
        self.export_cancel = button(actions, "Cancel", self.cancel_export)
        self.export_armed = False

    # -- widgets

    def _style(self) -> None:
        c = _colors()
        style = ttk.Style(self)
        style.theme_use("clam" if "clam" in style.theme_names() else style.theme_use())
        style.configure(
            "Treeview",
            background=c["bg"],
            fieldbackground=c["bg"],
            foreground=c["fg"],
            rowheight=24,
            borderwidth=0,
        )
        style.configure("Treeview.Heading", background=c["line"], foreground=c["fg"])
        style.map(
            "Treeview", background=[("selected", c["sel"])], foreground=[("selected", c["fg"])]
        )

    def _scrolled_tree(
        self, master: Any, columns: tuple[tuple[str, str, int], ...]
    ) -> ttk.Treeview:
        self._style()
        frame = ctk.CTkFrame(master, fg_color="transparent")
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(
            frame, columns=[c[0] for c in columns], show="headings", selectmode="browse"
        )
        for key, text, width in columns:
            tree.heading(key, text=text)
            tree.column(key, width=width, stretch=key in ("account", "communities", "members"))
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        return tree

    def _build_table(self, tab: Any) -> None:
        self.table = self._scrolled_tree(tab, self.COLUMNS)
        self.table.bind("<<TreeviewSelect>>", lambda _e: self._selection_changed())
        self.table.bind("<Return>", lambda _e: self.open_profile())
        self.table.bind("<Double-1>", lambda _e: self.open_profile())

    def _build_clusters(self, tab: Any) -> None:
        note(
            tab,
            "Accounts that stopped posting within minutes of each other. Each group "
            "has the same color in the map and the timeline.",
        ).pack(anchor="w", pady=4)
        self.cluster_table = self._scrolled_tree(
            tab,
            (
                ("cluster", "Stop group", 90),
                ("size", "Accounts", 80),
                ("first", "First stop (UTC)", 150),
                ("last", "Last stop (UTC)", 150),
                ("before", "Hours before cutoff", 130),
                ("marker", "Nearest marker", 150),
                ("members", "Members", 260),
            ),
        )

    def _build_timeline(self, tab: Any) -> None:
        note(
            tab,
            "Posts per day for each shown account. The red line is the cutoff; a "
            "potential bot posts steadily up to it and then nothing. The other lines "
            "are the event's markers, earlier moments of the outage (times as reported; "
            "dashed when approximate). The legend names each line.",
        ).pack(anchor="w", pady=4)
        frame = ctk.CTkFrame(tab, fg_color="transparent")
        frame.pack(fill="both", expand=True)
        self.timeline = tk.Canvas(frame, highlightthickness=0)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.timeline.yview)
        self.timeline.configure(yscrollcommand=scroll.set)
        self.timeline.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.timeline.bind("<Configure>", lambda _e: self.draw_timeline())
        self.timeline.bind("<MouseWheel>", self._wheel)
        self.timeline.bind("<Button-4>", lambda _e: self.timeline.yview_scroll(-3, "units"))
        self.timeline.bind("<Button-5>", lambda _e: self.timeline.yview_scroll(3, "units"))

    def _wheel(self, event: Any) -> None:
        step = event.delta / 120 if sys.platform.startswith("win") else event.delta
        self.timeline.yview_scroll(int(-step) or (-1 if event.delta > 0 else 1), "units")

    # -- data

    def on_show(self) -> None:
        self.refresh_runs()

    def refresh_runs(self) -> None:
        try:
            runs = P.list_runs(self.app.model.db, self.app.model.event_id)
        except Exception as exc:
            runs = []
            self.message.configure(
                text=f"Could not read the database: {exc}", text_color=ERROR_COLOR
            )
        self.run_ids = {P.run_label(r): int(r["run_id"]) for r in runs}
        labels = list(self.run_ids) or ["(no runs)"]
        current = self.run_menu.get()
        self.run_menu.configure(values=labels)
        wanted = self.app.model.last_run_id
        pick = next((lab for lab, rid in self.run_ids.items() if rid == wanted), None)
        self.run_menu.set(pick or (current if current in labels else labels[0]))
        self.reload()

    def select_run(self, run_id: int | None) -> None:
        self.app.model.last_run_id = run_id
        self.refresh_runs()

    def reload(self) -> None:
        self.disarm_export()
        run_id = self.run_ids.get(self.run_menu.get())
        self.results = None
        if run_id is not None:
            try:
                self.results = P.load_results(
                    run_id,
                    db=self.app.model.db,
                    hide_handles=self.app.model.hide_handles,
                    event_id=self.app.model.event_id,
                )
                self.message.configure(
                    text=f"{self.results.placebo} Outage event {self.results.event_id}, "
                    f"computed with the cutoff {P.format_cutoff(self.results.cutoff)}. "
                    + P.NOT_PROOF,
                    text_color=MUTED,
                )
            except (LookupError, ValueError) as exc:
                self.message.configure(
                    text=f"This run cannot be shown: {exc}", text_color=ERROR_COLOR
                )
        choices = P.filter_choices(self.results) if self.results else {}
        for key, menu in self.filter_menus.items():
            values = choices.get(key, [P.ALL])
            menu.configure(values=values)
            if menu.get() not in values:
                menu.set(P.ALL)
        self.apply_filters()

    def set_filter(self, key: str, value: str) -> None:
        self.filter_menus[key].set(value)
        self.apply_filters()

    def apply_filters(self) -> None:
        self.filters = P.Filters(**{k: m.get() for k, m in self.filter_menus.items()})
        self.shown = P.filter_accounts(self.results.accounts, self.filters) if self.results else []
        self.fill_table()
        self.fill_clusters()
        self.fill_summary()
        self.draw_graph()
        self.draw_timeline()
        self._selection_changed()

    def _color(self, account: P.FlaggedAccount) -> str | None:
        return P.cluster_color(account.cluster_id, self.results.clusters if self.results else [])

    def fill_table(self) -> None:
        self.table.delete(*self.table.get_children())
        for account in self.shown:
            color = self._color(account)
            tags = (f"c{account.cluster_id}",) if color else ()
            self.table.insert(
                "",
                "end",
                iid=P.account_key(account),
                tags=tags,
                values=(
                    account.rank,
                    account.label,
                    account.signals + (" (provisional)" if account.provisional else ""),
                    f"{account.score:.3f}",
                    account.cluster_id if account.cluster_id is not None else "-",
                    P.isoformat(account.last_activity)[:16].replace("T", " ")
                    if account.last_activity
                    else "-",
                    account.marker_text or "-",
                    f"{account.posts_per_day:.2f}",
                    ", ".join(account.communities) or "-",
                    ", ".join(account.also_in) or "-",
                ),
            )
            if color:
                self.table.tag_configure(tags[0], foreground=color)

    def fill_clusters(self) -> None:
        self.cluster_table.delete(*self.cluster_table.get_children())
        if not self.results:
            return
        keys = {P.account_key(a) for a in self.shown}
        for cluster in self.results.clusters:
            members = [a for a in self.results.accounts if a.cluster_id == cluster.cluster_id]
            if not any(P.account_key(a) in keys for a in members):
                continue
            tag = f"c{cluster.cluster_id}"
            self.cluster_table.insert(
                "",
                "end",
                tags=(tag,),
                values=(
                    cluster.cluster_id,
                    cluster.size,
                    P.isoformat(cluster.first_stop)[:16].replace("T", " ")
                    if cluster.first_stop
                    else "-",
                    P.isoformat(cluster.last_stop)[:16].replace("T", " ")
                    if cluster.last_stop
                    else "-",
                    f"{cluster.hours_before_cutoff:.1f}"
                    if cluster.hours_before_cutoff is not None
                    else "-",
                    cluster.marker or "-",
                    ", ".join(cluster.members),
                ),
            )
            color = P.cluster_color(cluster.cluster_id, self.results.clusters)
            if color:
                self.cluster_table.tag_configure(tag, foreground=color)

    def fill_summary(self) -> None:
        if not self.results:
            set_text(
                self.summary, "Choose a run above. Runs appear here once a scan has stored them."
            )
            set_text(self.details, "Choose a run above to see its outage event.")
            return
        set_text(self.details, self.results.details)
        r = self.results
        lines = [
            f"Run {r.run_id} ({r.platform}, {r.status})",
            f"Outage event: {r.event_id}",
            f"Cutoff these results were computed with: {P.format_cutoff(r.cutoff)}",
            "",
        ]
        elsewhere = [a for a in r.accounts if a.also_in]
        if elsewhere:
            lines += [
                f"{len(elsewhere)} flagged accounts also appear in the scans of another "
                'event (column "Also in event"); each event keeps its own results.',
                "",
            ]
        lines += [f"{label}: {value}" for label, value in r.summary]
        lines += [
            "",
            f"Shown with the filters: {len(self.shown)} of {len(r.accounts)} flagged accounts.",
            "",
        ]
        lines += [f"Note: {text}" for text in r.notices]
        lines += [
            "",
            "Signals: N counts the signals measured on the baseline, independent of the "
            "outage's timing (very regular timing, posting around the clock). A "
            "synchronized stop is not counted: the reported outage also stopped ordinary "
            "users at the same moment. No count is proof of anything.",
        ]
        set_text(self.summary, "\n".join(lines))

    def selected(self) -> P.FlaggedAccount | None:
        chosen = self.table.selection()
        if not chosen:
            return None
        return next((a for a in self.shown if P.account_key(a) == chosen[0]), None)

    def _selection_changed(self) -> None:
        account = self.selected()
        self.profile_button.configure(
            state="normal" if account and account.profile_url else "disabled"
        )
        community = self._selected_community()
        self.community_button.configure(
            state="normal" if community and community.url else "disabled"
        )

    def _selected_community(self) -> P.Community | None:
        if not self.results:
            return None
        name = self.filters.community
        if name == P.ALL:
            account = self.selected()
            name = account.communities[0] if account and account.communities else P.ALL
        return self.results.communities.get(name)

    def open_profile(self) -> None:
        account = self.selected()
        if account is not None and not self.app.model.hide_handles:
            P.open_link(account.profile_url)

    def open_community(self) -> None:
        community = self._selected_community()
        if community is not None:
            P.open_link(community.url)

    def export(self) -> None:
        """Export in two steps: the first press warns, the second asks for the file."""
        if not self.results:
            return
        if not self.export_armed:
            self.export_armed = True
            self.export_button.configure(text="Confirm: choose a file...")
            self.export_cancel.pack(side="left")
            self.message.configure(text=P.EXPORT_WARNING, text_color=ERROR_COLOR)
            return
        self.disarm_export()
        path = filedialog.asksaveasfilename(
            title="Export the run as CSV",
            defaultextension=".csv",
            initialdir=str(self.app.model.output_dir),
            initialfile=f"bot-farm-silence-tracker-run-{self.results.run_id}-ids.csv",
            filetypes=[("CSV", "*.csv")],
        )
        if not path:
            return
        try:
            count = P.export_csv(
                self.results.run_id,
                Path(path),
                db=self.app.model.db,
                event_id=self.app.model.event_id,
            )
        except (OSError, LookupError, ValueError) as exc:
            self.message.configure(text=f"Not exported: {exc}", text_color=ERROR_COLOR)
            return
        self.message.configure(
            text=f"Exported {count} rows to {path}. The file holds real "
            "account names: keep it private.",
            text_color=MUTED,
        )

    def disarm_export(self) -> None:
        self.export_armed = False
        self.export_button.configure(text="Export CSV...")
        self.export_cancel.pack_forget()

    def cancel_export(self) -> None:
        self.disarm_export()
        self.message.configure(text="Export cancelled. Nothing was written.", text_color=MUTED)

    # -- drawing

    def draw_graph(self) -> None:
        canvas = self.graph
        c = _colors()
        canvas.configure(background=c["bg"])
        canvas.delete("all")
        width, height = max(canvas.winfo_width(), 400), max(canvas.winfo_height(), 300)
        if not self.results or not self.shown:
            canvas.create_text(
                width / 2, height / 2, fill=c["muted"], text="No flagged accounts to show."
            )
            return
        graph = P.build_graph(self.results, self.filters)
        height -= 30  # Room for the caption.
        limit = max(1, int((height - 40) // 16))
        accounts = graph.accounts[:limit]
        communities = graph.communities[: max(1, int((height - 40) // 34))]
        ax, cx = 150, width - 230
        apos = {
            n.key: 30 + i * (height - 60) / max(1, len(accounts) - 1 or 1)
            for i, n in enumerate(accounts)
        }
        cpos = {
            n.key: 30 + i * (height - 60) / max(1, len(communities) - 1 or 1)
            for i, n in enumerate(communities)
        }
        clusters = self.results.clusters
        for akey, ckey in graph.edges:
            if akey in apos and ckey in cpos:
                node = next(n for n in accounts if n.key == akey)
                color = P.cluster_color(node.cluster_id, clusters) or c["line"]
                canvas.create_line(ax + 6, apos[akey], cx - 10, cpos[ckey], fill=color)
        for node in accounts:
            y = apos[node.key]
            color = P.cluster_color(node.cluster_id, clusters) or c["node"]
            canvas.create_oval(ax - 5, y - 5, ax + 5, y + 5, fill=color, outline=color)
            canvas.create_text(
                ax - 12,
                y,
                text=node.label[:20],
                anchor="e",
                fill=c["fg"],
                font=("TkDefaultFont", 10),
            )
        biggest = max(n.size for n in communities)
        for index, node in enumerate(communities):
            y = cpos[node.key]
            radius = 6 + 14 * (node.size / biggest) ** 0.5
            tag = f"community{index}"
            active = self.filters.community == node.key
            canvas.create_oval(
                cx - radius,
                y - radius,
                cx + radius,
                y + radius,
                fill=c["bar"],
                outline=c["cut"] if active else c["bar"],
                width=3 if active else 1,
                tags=(tag,),
            )
            canvas.create_text(
                cx + radius + 8,
                y,
                text=f"{node.label[:28]} ({node.size})",
                anchor="w",
                fill=c["fg"],
                tags=(tag,),
            )
            canvas.tag_bind(tag, "<Button-1>", lambda _e, k=node.key: self._toggle_community(k))
        more = len(graph.accounts) - len(accounts)
        extra = f"Showing {len(accounts)} of {len(graph.accounts)} accounts. " if more > 0 else ""
        canvas.create_text(
            10,
            height + 24,
            anchor="sw",
            fill=c["muted"],
            width=width - 20,
            text=f"{extra}Click a community to show only its accounts; "
            "click it again to show all. Circle size: flagged accounts.",
        )

    def _toggle_community(self, name: str) -> None:
        self.set_filter("community", P.ALL if self.filters.community == name else name)

    def draw_timeline(self) -> None:
        canvas = self.timeline
        c = _colors()
        canvas.configure(background=c["bg"])
        canvas.delete("all")
        width = max(canvas.winfo_width(), 400)
        if not self.results or not self.shown:
            canvas.create_text(width / 2, 60, fill=c["muted"], text="No flagged accounts to show.")
            canvas.configure(scrollregion=(0, 0, width, 120))
            return
        rows = self.shown[:200]
        row_h, left, right = 34, 170, 16
        days = max(len(a.timeline.counts) for a in rows)
        step = (width - left - right) / max(1, days)
        peak = max(1, *(max(a.timeline.counts or (0,)) for a in rows))
        lines = self.results.lines
        legend_h = 16 * len(lines)
        first_top = 22 + legend_h

        def line_style(line: P.TimelineLine) -> dict[str, Any]:
            style: dict[str, Any] = {
                "fill": c["cut"] if line.kind == "cutoff" else c["marker"],
                "width": 2,
            }
            if line.approximate:
                style["dash"] = (5, 3)
            return style

        def line_tags(line: P.TimelineLine) -> tuple[str, ...]:
            return ("timeline-line", line.kind, *(("approx",) if line.approximate else ()))

        for i, account in enumerate(rows):
            top = first_top + i * row_h
            base = top + row_h - 8
            color = self._color(account) or c["bar"]
            canvas.create_text(
                left - 8,
                top + row_h / 2,
                anchor="e",
                fill=c["fg"],
                text=account.label[:22],
                font=("TkDefaultFont", 10),
            )
            for d, count in enumerate(account.timeline.counts):
                if count:
                    h = (row_h - 12) * count / peak
                    x = left + d * step
                    canvas.create_rectangle(
                        x, base - h, x + max(1, step - 1), base, fill=color, outline=""
                    )
            canvas.create_line(left, base, width - right, base, fill=c["line"])
            for line in lines:
                x = left + line.day * step
                canvas.create_line(
                    x, top + 2, x, base + 2, tags=line_tags(line), **line_style(line)
                )
        if account_count := len(self.shown) - len(rows):
            canvas.create_text(
                left,
                first_top + 2 + len(rows) * row_h,
                anchor="nw",
                fill=c["muted"],
                text=f"and {account_count} more; use the filters to narrow.",
            )
        first = rows[0].timeline
        canvas.create_text(
            left,
            4,
            anchor="nw",
            fill=c["muted"],
            font=("TkDefaultFont", 9),
            text=first.start.strftime("%Y-%m-%d"),
        )
        # The legend: one entry per line, the cutoff first, then the markers.
        for index, line in enumerate(lines):
            y = 26 + index * 16
            x = left + 90
            canvas.create_line(x, y, x + 28, y, tags=("legend", line.kind), **line_style(line))
            canvas.create_text(
                x + 36,
                y,
                anchor="w",
                fill=c["fg"],
                font=("TkDefaultFont", 9),
                text=line.label,
                tags=("legend",),
            )
        total_h = first_top + 32 + len(rows) * row_h
        canvas.configure(scrollregion=(0, 0, width, total_h))


class NetworkScreen(Screen):
    def __init__(self, master: Any, app: App) -> None:
        super().__init__(master, app)
        heading(self, "Network and privacy").pack(padx=24, pady=(20, 4), anchor="w")
        note(
            self,
            "Which hosts each platform contacts and which credentials go where. "
            "This is the same report as `bot-farm-silence-tracker network`, built from "
            "the list the program enforces: every other host is refused.",
        ).pack(padx=24, anchor="w")
        self.text = ctk.CTkTextbox(self, wrap="word", font=ctk.CTkFont(family="Courier", size=12))
        self.text.pack(fill="both", expand=True, padx=24, pady=12)
        set_text(self.text, P.network_text())


# -- entry points --------------------------------------------------------------------


def run_app(*, selftest: bool = False) -> int:
    if selftest:
        return run_selftest()
    app = App()
    app.mainloop()
    return 0


def sample_run(folder: Path, event_id: str | None = None) -> tuple[P.AppState, int]:
    """Scan 200 accounts of the offline ``fake`` platform into ``folder``.

    ``event_id`` names the outage event (default: the app's default event). Returns
    the app state pointing at that database and the run id. Makes no network request
    and touches neither the user's database nor their results folder.
    """
    model = P.AppState(output_dir=folder, db=folder / "selftest.db")
    form = P.ScanForm(platform="fake", n="200")
    if event_id is not None:
        event = P.outage_events.load_events().resolve(event_id)
        form = P.ScanForm(platform="fake", n="200", event=event.id, cutoff=event.cutoff)
    request, problems = P.validate_form(form)
    if request is None:
        raise RuntimeError(f"the self-test scan settings are invalid: {problems}")
    job = P.ScanJob(
        [*P.scan_argv(request, out=folder / "selftest.csv", db=model.db), "--seed", "1"],
        db=model.db,
    )
    job.start()
    job.join()
    finished = [e for e in job.drain(limit=100_000) if isinstance(e, P.Finished)]
    if not finished or finished[-1].run_id is None or finished[-1].code != 0:
        raise RuntimeError("the self-test scan did not finish")
    return model, finished[-1].run_id


RESULT_TABS = (
    "Flagged accounts",
    "Stop groups",
    "Account \u2194 community map",
    "Silence timeline",
    "Summary",
    "Outage event",
)


def exercise(app: App, run_id: int) -> None:
    """Show every screen and every results view of ``run_id``, with and without
    hidden handles and with a community filter. Raises when something is missing."""
    app.update()
    for key, _label in SCREENS:
        app.show(key)
        app.update()
    scan = app.screens["scan"]
    default = P.event_choices()[0]
    scan._event_chosen(default.label)
    if scan.picker.get() != default.cutoff:
        raise RuntimeError("the event menu did not set the cutoff picker")
    scan.picker.set(default.cutoff - P.timedelta(days=1, hours=3))
    if scan.picker.local.cget("text") != P.local_picker_line(scan.picker.get()):
        raise RuntimeError("the cutoff picker does not show the local time")
    scan._cutoff_changed()
    if scan.event.get() != P.CUSTOM_EVENT.label:
        raise RuntimeError("a picked cutoff did not switch the event menu to a custom date")
    scan._event_chosen(default.label)
    results = app.screens["results"]
    results.select_run(run_id)
    if not results.results or not results.results.accounts:
        raise RuntimeError("the self-test scan flagged no accounts")
    for tab in RESULT_TABS:
        results.tabs.set(tab)
        app.update()
    for name in (next(iter(results.results.communities)), P.ALL):
        results.set_filter("community", name)
        app.update()
    check_timeline_lines(results)
    for name in P.filter_choices(results.results)["marker"]:
        results.set_filter("marker", name)
        app.update()
    results.set_filter("marker", P.ALL)
    for hidden in (True, False):
        app.hide_var.set(hidden)
        app._hide_changed()
        app.update()


def check_timeline_lines(results: ResultsScreen) -> None:
    """Raise unless the timeline draws one line per row for the cutoff and each marker
    (dashed for an approximate one) and a legend entry for each."""
    assert results.results is not None
    canvas = results.timeline
    results.tabs.set("Silence timeline")
    results.draw_timeline()
    rows = min(len(results.shown), 200)
    lines = results.results.lines
    drawn = canvas.find_withtag("timeline-line")
    if len(drawn) != rows * len(lines):
        raise RuntimeError(f"the timeline drew {len(drawn)} lines, not {rows * len(lines)}")
    approximate = sum(1 for line in lines if line.approximate)
    if len(canvas.find_withtag("approx")) != rows * approximate:
        raise RuntimeError("the timeline did not draw the approximate markers dashed")
    if len(canvas.find_withtag("legend")) != 2 * len(lines):
        raise RuntimeError("the timeline's legend does not name every line")


def run_selftest() -> int:
    """Build the (hidden) window, render every screen with a small offline sample
    scan, close it. Prints the Tk version, which the release checks log."""
    import tkinter

    with tempfile.TemporaryDirectory(prefix="bfst-gui-selftest-") as tmp:
        model, run_id = sample_run(Path(tmp))
        app = App(model, hidden=True)
        try:
            patchlevel = app.tk.call("info", "patchlevel")
            print(
                f"Tk {patchlevel} (built for {tkinter.TkVersion}); CustomTkinter {ctk.__version__}"
            )
            exercise(app, run_id)
        finally:
            app.close()
    print("gui selftest ok")
    return 0
