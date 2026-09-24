"""Tkinter front end for the Amazon Photos Uploader."""
from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from photos_uploader import core

APP_NAME = "Amazon Photos Uploader"


class QueueHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record):
        self.q.put(("log", record.levelno, self.format(record)))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.minsize(560, 620)
        self.settings = core.load_settings()
        self.q: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel = threading.Event()

        handler = QueueHandler(self.q)
        handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        core.log.addHandler(handler)
        core.log.setLevel(logging.INFO)

        self._build()
        self._refresh()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._pump)

    # ------------------------------------------------------------------ layout
    def _build(self):
        pad = {"padx": 12, "pady": 6}
        root = ttk.Frame(self)
        root.pack(fill="both", expand=True)

        acct = ttk.LabelFrame(root, text="Amazon account")
        acct.pack(fill="x", **pad)
        self.status = ttk.Label(acct, text="")
        self.status.grid(row=0, column=0, sticky="w", padx=8, pady=8)
        self.btn_in = ttk.Button(acct, text="Sign in", command=self._sign_in)
        self.btn_in.grid(row=0, column=1, padx=4)
        self.btn_out = ttk.Button(acct, text="Sign out", command=self._sign_out)
        self.btn_out.grid(row=0, column=2, padx=(4, 8))
        ttk.Label(acct, text="Region:").grid(row=1, column=0, sticky="e", padx=8, pady=(0, 8))
        self.region = tk.StringVar(value=self.settings["region"])
        self.region_box = ttk.Combobox(acct, textvariable=self.region, values=core.REGIONS, width=10, state="readonly")
        self.region_box.grid(row=1, column=1, sticky="w", pady=(0, 8))
        self.region_box.bind("<<ComboboxSelected>>", lambda _e: self._region_changed())
        acct.columnconfigure(0, weight=1)

        fr = ttk.LabelFrame(root, text="Folders to upload (subfolders included)")
        fr.pack(fill="x", **pad)
        self.folders = tk.Listbox(fr, height=5, activestyle="none", selectmode="extended")
        self.folders.pack(side="left", fill="both", expand=True, padx=(8, 4), pady=8)
        col = ttk.Frame(fr)
        col.pack(side="right", padx=(4, 8), pady=8, anchor="n")
        self.btn_add = ttk.Button(col, text="Add folder…", command=self._add_folder)
        self.btn_add.pack(fill="x")
        self.btn_rm = ttk.Button(col, text="Remove", command=self._remove_folder)
        self.btn_rm.pack(fill="x", pady=(4, 0))

        opts = ttk.Frame(root)
        opts.pack(fill="x", **pad)
        self.videos = tk.BooleanVar(value=self.settings["include_videos"])
        self.dry = tk.BooleanVar(value=False)
        self.chk_v = ttk.Checkbutton(opts, text="Include videos", variable=self.videos, command=self._save)
        self.chk_v.pack(side="left")
        self.chk_d = ttk.Checkbutton(opts, text="Preview only (don't upload)", variable=self.dry)
        self.chk_d.pack(side="left", padx=16)

        run = ttk.Frame(root)
        run.pack(fill="x", **pad)
        self.btn_go = ttk.Button(run, text="Start upload", command=self._start)
        self.btn_go.pack(side="left")
        self.btn_cancel = ttk.Button(run, text="Cancel", command=self._cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=8)
        self.progress = ttk.Progressbar(run, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=(8, 0))

        lg = ttk.Frame(root)
        lg.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.log = tk.Text(lg, height=10, state="disabled", wrap="word")
        sb = ttk.Scrollbar(lg, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.tag_configure("warn", foreground="#b45309")
        self.log.tag_configure("error", foreground="#b91c1c")
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

        self.locked = [self.btn_in, self.btn_out, self.region_box, self.btn_add, self.btn_rm,
                       self.chk_v, self.chk_d, self.btn_go]

    # ------------------------------------------------------------------ state
    def _save(self):
        self.settings["include_videos"] = self.videos.get()
        self.settings["region"] = self.region.get()
        core.save_settings(self.settings)

    def _refresh(self):
        s = self.settings
        if s["logged_in"]:
            self.status.config(text="● Signed in", foreground="#15803d")
        else:
            self.status.config(text="○ Not signed in", foreground="#b91c1c")
        self.btn_in.config(text="Re-sign in" if s["logged_in"] else "Sign in")
        self.btn_out.config(state="normal" if s["logged_in"] else "disabled")
        self.folders.delete(0, "end")
        for f in s["folders"]:
            self.folders.insert("end", f)

    def _set_busy(self, busy: bool):
        for w in self.locked:
            w.config(state="disabled" if busy else "normal")
        if not busy:
            self.region_box.config(state="readonly")
            self.btn_out.config(state="normal" if self.settings["logged_in"] else "disabled")
        self.btn_cancel.config(state="normal" if busy else "disabled")

    def _write_log(self, text: str, level: int = logging.INFO):
        tag = "error" if level >= logging.ERROR else "warn" if level >= logging.WARNING else ""
        self.log.config(state="normal")
        self.log.insert("end", text + "\n", tag)
        self.log.see("end")
        self.log.config(state="disabled")

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

    def _remove_folder(self):
        for i in reversed(self.folders.curselection()):
            del self.settings["folders"][i]
        self._save()
        self._refresh()

    def _sign_in(self):
        self._save()
        self._run(lambda: core.sign_in(self.settings, self.cancel), on_result="signin", indeterminate=True)

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
        self._run(
            lambda: core.upload(settings, dry, self.cancel,
                                lambda done, total: self.q.put(("progress", done, total))),
            on_result="upload", indeterminate=True,
        )

    def _cancel(self):
        self.cancel.set()
        self._write_log("Cancelling after the current step (a batch already handed to Amazon finishes first)...",
                        logging.WARNING)
        self.btn_cancel.config(state="disabled")

    def _run(self, fn, on_result: str, indeterminate: bool):
        self.cancel.clear()
        self._set_busy(True)
        if indeterminate:
            self.progress.config(mode="indeterminate")
            self.progress.start(12)

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
                    self.progress.config(mode="determinate", maximum=max(total, 1), value=done)
                elif msg[0] == "done":
                    self._finish(*msg[1:])
        except queue.Empty:
            pass
        self.after(100, self._pump)

    def _finish(self, kind: str, result, error):
        self.progress.stop()
        self.progress.config(mode="determinate", value=0)
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
