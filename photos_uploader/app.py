"""CustomTkinter front end for the Amazon Photos Uploader."""
from __future__ import annotations

import logging
import queue
import threading
from tkinter import filedialog, messagebox

import customtkinter as ctk

from photos_uploader import core

APP_NAME = "Amazon Photos Uploader"
UPLOAD_HINT = ("A browser window is doing the uploading for you. Leave it open and visible - don't close it, "
               "click, or type in it. Keep this computer awake until it finishes.")

# Palette shared with the LCD Labs site.
BG = "#F7F6FA"
CARD = "#FFFFFF"
INK = "#171623"
MUTED = "#6E6C7C"
BORDER = "#E4E2EC"
ACCENT = "#5B4CFF"
ACCENT_HOVER = "#4A3CE6"
GOOD, GOOD_BG = "#15803D", "#DCFCE7"
BAD, BAD_BG = "#B91C1C", "#FEE2E2"
WARN = "#B45309"


class QueueHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record):
        self.q.put(("log", record.levelno, self.format(record)))


def card(parent, title: str) -> ctk.CTkFrame:
    frame = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=14, border_width=1, border_color=BORDER)
    frame.pack(fill="x", padx=20, pady=(0, 12))
    ctk.CTkLabel(frame, text=title, font=ctk.CTkFont(size=13, weight="bold"), text_color=MUTED
                 ).pack(anchor="w", padx=16, pady=(12, 4))
    return frame


def primary(parent, text, command, **kw) -> ctk.CTkButton:
    kw.setdefault("height", 36)
    return ctk.CTkButton(parent, text=text, command=command, corner_radius=10,
                         fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="white",
                         font=ctk.CTkFont(size=14, weight="bold"), **kw)


def secondary(parent, text, command, **kw) -> ctk.CTkButton:
    kw.setdefault("height", 36)
    return ctk.CTkButton(parent, text=text, command=command, corner_radius=10,
                         fg_color="transparent", hover_color="#EEEDF6", text_color=INK,
                         border_width=1, border_color=BORDER, font=ctk.CTkFont(size=13), **kw)


class App(ctk.CTk):
    def __init__(self):
        ctk.set_appearance_mode("light")
        super().__init__(fg_color=BG)
        self.title(APP_NAME)
        self.geometry("620x800")
        self.minsize(560, 720)
        self.settings = core.load_settings()
        self.q: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel = threading.Event()
        self.row_buttons: list[ctk.CTkButton] = []

        handler = QueueHandler(self.q)
        handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        handler.setLevel(logging.INFO)
        core.log.addHandler(handler)
        core.log.setLevel(logging.DEBUG)
        try:  # detailed diagnostics for troubleshooting: <app data folder>/uploader.log
            from logging.handlers import RotatingFileHandler
            fh = RotatingFileHandler(core.app_dir() / "uploader.log", maxBytes=500_000, backupCount=1, encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            core.log.addHandler(fh)
        except OSError:
            pass

        self._build()
        self._refresh()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._pump)

    # ------------------------------------------------------------------ layout
    def _build(self):
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=24, pady=(20, 14))
        ctk.CTkLabel(head, text="Amazon Photos Uploader", text_color=INK,
                     font=ctk.CTkFont(size=22, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(head, text="Upload a folder of photos, skipping anything already uploaded.",
                     text_color=MUTED, font=ctk.CTkFont(size=13)).pack(anchor="w")

        # Account
        acct = card(self, "AMAZON ACCOUNT")
        row = ctk.CTkFrame(acct, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 6))
        self.status = ctk.CTkLabel(row, text="", corner_radius=999, height=28, width=130,
                                   font=ctk.CTkFont(size=13, weight="bold"))
        self.status.pack(side="left")
        self.btn_out = secondary(row, "Sign out", self._sign_out, width=90)
        self.btn_out.pack(side="right")
        self.btn_in = primary(row, "Sign in", self._sign_in, width=110)
        self.btn_in.pack(side="right", padx=(0, 8))

        reg = ctk.CTkFrame(acct, fg_color="transparent")
        reg.pack(fill="x", padx=16, pady=(0, 14))
        ctk.CTkLabel(reg, text="Amazon region", text_color=MUTED, font=ctk.CTkFont(size=13)).pack(side="left")
        self.region = ctk.StringVar(value=self.settings["region"])
        self.region_box = ctk.CTkOptionMenu(
            reg, variable=self.region, values=core.REGIONS,
            command=lambda _v: self._region_changed(), width=110, height=30, corner_radius=8,
            fg_color=BG, button_color=BORDER, button_hover_color="#D6D3E3", text_color=INK,
            dropdown_fg_color=CARD, dropdown_text_color=INK, dropdown_hover_color="#EEEDF6")
        self.region_box.pack(side="left", padx=10)

        self.hint = ctk.CTkLabel(acct, text="", text_color=ACCENT, fg_color="#EEEBFF", corner_radius=8,
                                 justify="left", anchor="w", wraplength=520, font=ctk.CTkFont(size=13))

        # Folders
        fol = card(self, "FOLDERS  ·  subfolders are included")
        self.folder_list = ctk.CTkFrame(fol, fg_color=BG, corner_radius=10)
        self.folder_list.pack(fill="x", padx=16, pady=(0, 8), ipady=2)
        self.btn_add = secondary(fol, "+  Add folder", self._add_folder, width=130)
        self.btn_add.pack(anchor="w", padx=16, pady=(0, 14))

        # Options
        opt = card(self, "OPTIONS")
        self.videos = ctk.BooleanVar(value=self.settings["include_videos"])
        self.dry = ctk.BooleanVar(value=False)
        self.sw_v = ctk.CTkSwitch(opt, text="Include videos", variable=self.videos, command=self._save,
                                  progress_color=ACCENT, text_color=INK)
        self.sw_v.pack(anchor="w", padx=16, pady=(2, 6))
        self.sw_d = ctk.CTkSwitch(opt, text="Preview only (don't upload anything)", variable=self.dry,
                                  progress_color=ACCENT, text_color=INK)
        self.sw_d.pack(anchor="w", padx=16, pady=(0, 14))

        # Run
        run = ctk.CTkFrame(self, fg_color="transparent")
        run.pack(fill="x", padx=20, pady=(0, 10))
        self.btn_go = primary(run, "Start upload", self._start, width=140, height=42)
        self.btn_go.pack(side="left")
        self.btn_cancel = secondary(run, "Cancel", self._cancel, width=90, height=42, state="disabled")
        self.btn_cancel.pack(side="left", padx=8)
        self.progress = ctk.CTkProgressBar(run, height=10, corner_radius=5, progress_color=ACCENT, fg_color=BORDER)
        self.progress.pack(side="left", fill="x", expand=True, padx=(10, 0))
        self.progress.set(0)

        # Log
        self.log = ctk.CTkTextbox(self, fg_color=CARD, border_width=1, border_color=BORDER, corner_radius=12,
                                  text_color=INK, font=ctk.CTkFont(family="Consolas", size=12), wrap="word")
        self.log.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        self.log.tag_config("warn", foreground=WARN)
        self.log.tag_config("error", foreground=BAD)
        self.log.configure(state="disabled")

        self.locked = [self.btn_in, self.region_box, self.btn_add, self.sw_v, self.sw_d, self.btn_go]

    # ------------------------------------------------------------------ state
    def _save(self):
        self.settings["include_videos"] = self.videos.get()
        self.settings["region"] = self.region.get()
        core.save_settings(self.settings)

    def _refresh(self):
        s = self.settings
        if s["logged_in"]:
            self.status.configure(text="●  Signed in", text_color=GOOD, fg_color=GOOD_BG)
        else:
            self.status.configure(text="○  Not signed in", text_color=BAD, fg_color=BAD_BG)
        self.btn_in.configure(text="Re-sign in" if s["logged_in"] else "Sign in")
        self.btn_out.configure(state="normal" if s["logged_in"] else "disabled")

        for w in self.folder_list.winfo_children():
            w.destroy()
        self.row_buttons = []
        if not s["folders"]:
            ctk.CTkLabel(self.folder_list, text="No folders yet - add one below.", text_color=MUTED
                         ).pack(pady=18)
        for i, f in enumerate(s["folders"]):
            r = ctk.CTkFrame(self.folder_list, fg_color=CARD, corner_radius=8)
            r.pack(fill="x", pady=2)
            ctk.CTkLabel(r, text=f, text_color=INK, anchor="w", font=ctk.CTkFont(size=12)
                         ).pack(side="left", fill="x", expand=True, padx=10, pady=6)
            b = ctk.CTkButton(r, text="✕", width=28, height=26, corner_radius=6, fg_color="transparent",
                              hover_color=BAD_BG, text_color=MUTED, command=lambda i=i: self._remove_folder(i))
            b.pack(side="right", padx=4)
            self.row_buttons.append(b)

    def _set_busy(self, busy: bool):
        state = "disabled" if busy else "normal"
        for w in [*self.locked, *self.row_buttons]:
            w.configure(state=state)
        if not busy:
            self.btn_out.configure(state="normal" if self.settings["logged_in"] else "disabled")
        else:
            self.btn_out.configure(state="disabled")
        self.btn_cancel.configure(state="normal" if busy else "disabled")

    def _write_log(self, text: str, level: int = logging.INFO):
        tag = "error" if level >= logging.ERROR else "warn" if level >= logging.WARNING else None
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    # ------------------------------------------------------------------ actions
    def _region_changed(self):
        if self.region.get() != self.settings["region"]:
            self._save()
            if self.settings["logged_in"]:
                self._write_log("Region changed - sign in again for the new region.", logging.WARNING)

    def _add_folder(self):
        d = filedialog.askdirectory(title="Choose a folder of photos")
        if d and d not in self.settings["folders"]:
            self.settings["folders"].append(d)
            self._save()
            self._refresh()

    def _remove_folder(self, i: int):
        del self.settings["folders"][i]
        self._save()
        self._refresh()

    def _sign_in(self):
        self._save()
        if not messagebox.askokcancel(
            APP_NAME,
            "A browser window will open. To finish signing in:\n\n"
            "1.  Sign in to your Amazon account (including any verification).\n"
            "2.  Keep going until you can see your Photos library.\n"
            "3.  The window closes by itself once you're there - don't close it early.",
        ):
            return
        settings = dict(self.settings)
        self._run(lambda: core.sign_in(settings, self.cancel), on_result="signin",
                  hint="Sign in to Amazon in the browser window, then continue until you can see your "
                       "Photos library. The window closes by itself when you're done.")

    def _sign_out(self):
        if messagebox.askyesno(APP_NAME, "Sign out and forget the saved Amazon session?"):
            self.settings = core.sign_out(self.settings)
            core.save_settings(self.settings)
            self._refresh()
            self._write_log("Signed out.")

    def _start(self):
        self._save()
        if not self.settings["folders"]:
            messagebox.showinfo(APP_NAME, "Add at least one folder first.")
            return
        if not self.settings["logged_in"] and not self.dry.get():
            messagebox.showinfo(APP_NAME, "Sign in to Amazon first.")
            return
        settings, dry = dict(self.settings), self.dry.get()
        if not dry and not messagebox.askokcancel(
            APP_NAME,
            "Heads up - this app uploads by driving a real browser window.\n\n"
            "When you click OK:\n"
            "  -  A Chrome/Edge window opens and the app clicks through Amazon Photos for you.\n"
            "  -  Leave that window open and visible. Don't close it, click, or type in it.\n"
            "  -  Keep your computer awake and connected until it finishes.\n\n"
            "You can use other programs meanwhile. If the window gets closed by accident, nothing is "
            "lost: click Start upload again and it picks up where it left off.",
        ):
            return
        self._run(
            lambda: core.upload(settings, dry, self.cancel,
                                lambda done, total: self.q.put(("progress", done, total))),
            on_result="upload",
            hint=None if dry else UPLOAD_HINT,
        )

    def _cancel(self):
        self.cancel.set()
        self._write_log("Cancelling after the current step (a batch already handed to Amazon finishes first)...",
                        logging.WARNING)
        self.btn_cancel.configure(state="disabled")

    def _run(self, fn, on_result: str, hint: str | None = None):
        self.cancel.clear()
        self._set_busy(True)
        if hint:
            self.hint.configure(text="  " + hint + "  ")
            self.hint.pack(fill="x", padx=16, pady=(0, 14), ipady=8)
        self.progress.configure(mode="indeterminate")
        self.progress.start()

        def target():
            try:
                self.q.put(("done", on_result, fn(), None))
            except core.Cancelled:
                self.q.put(("done", on_result, None, "cancelled"))
            except core.NotLoggedIn as e:
                self.q.put(("done", on_result, None, ("auth", str(e))))
            except core.UploadFailed as e:
                self.q.put(("done", on_result, None, ("fail", str(e))))
            except Exception as e:  # never let the worker die silently
                core.log.exception("Unexpected error")
                self.q.put(("done", on_result, None, ("fail", f"Unexpected error: {e}")))

        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()

    # ------------------------------------------------------------------ worker -> UI
    def _pump(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "log":
                    self._write_log(msg[2], msg[1])
                elif msg[0] == "progress":
                    _, done, total = msg
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self.progress.set(done / max(total, 1))
                elif msg[0] == "done":
                    self._finish(*msg[1:])
        except queue.Empty:
            pass
        self.after(100, self._pump)

    def _finish(self, kind: str, result, error):
        self.hint.pack_forget()
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.progress.set(1 if (kind == "upload" and not error and not self.dry.get()) else 0)
        self._set_busy(False)
        if error == "cancelled":
            self._write_log("Cancelled. Anything already uploaded is remembered, so you can resume any time.")
        elif error:
            what, text = error
            if what == "auth":
                self.settings["logged_in"] = False
                core.save_settings(self.settings)
            self._write_log(text, logging.ERROR)
            messagebox.showerror(APP_NAME, text)
        elif kind == "signin":
            self.settings = result
            core.save_settings(self.settings)
            self._write_log("Signed in.")
        elif kind == "upload":
            if self.dry.get():
                self._write_log(f"Preview: {result['to_upload']} file(s) would upload, {result['skipped']} already done.")
            else:
                self._write_log(f"Done. Uploaded {result['uploaded']} file(s); {result['skipped']} were already done.")
        self._refresh()

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(APP_NAME, "An operation is running. Quit anyway?\n"
                                                 "(Finished uploads are remembered.)"):
                return
        self.destroy()


def main():
    App().mainloop()
