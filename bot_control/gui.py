import os
import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk

from .manager import LABELS, Manager, StartError


class InstanceLock:
    def __init__(self, path):
        self.path = path
        self.file = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.file.close()
            return False


class Panel:
    def __init__(self, window, *, autostart=False):
        self.window = window
        self.events = queue.Queue()
        self.manager = Manager(notify=self.events.put)
        self.busy = False
        self.window.title("Боти — запуск одним кліком")
        self.window.geometry("740x740")
        self.window.minsize(680, 720)
        self.window.configure(bg="#f3f6fb")
        style = ttk.Style(window)
        style.theme_use("clam")
        style.configure("TFrame", background="#f3f6fb")
        style.configure("TLabel", background="#f3f6fb", foreground="#172b45", font=("Segoe UI", 11))
        style.configure("Title.TLabel", font=("Segoe UI", 24, "bold"))
        style.configure("Muted.TLabel", foreground="#64748b", font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 11), padding=(14, 10))
        style.configure(
            "Start.TButton",
            font=("Segoe UI", 13, "bold"),
            background="#146c43",
            foreground="white",
            padding=(20, 14),
        )
        style.map("Start.TButton", background=[("active", "#115c39"), ("disabled", "#9aaca3")])
        outer = ttk.Frame(window, padding=28)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Боти під контролем", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer, text="Один запуск для планнера й українізатора.", style="Muted.TLabel"
        ).pack(anchor="w", pady=(6, 22))
        self.rows = {}
        for name in ("libre", "gateway", "ngrok", "planner", "translator"):
            row = ttk.Frame(outer, padding=(0, 9))
            row.pack(fill="x")
            dot = tk.Label(row, text="●", fg="#94a3b8", bg="#f3f6fb", font=("Segoe UI", 15))
            dot.pack(side="left", padx=(0, 12))
            ttk.Label(row, text=LABELS[name]).pack(side="left")
            value = ttk.Label(row, text="Перевіряємо…", style="Muted.TLabel")
            value.pack(side="right")
            self.rows[name] = (dot, value)
        self.message = tk.StringVar(value="Натисніть «Запустити все» — решту панель зробить сама.")
        ttk.Label(outer, textvariable=self.message, wraplength=650).pack(anchor="w", pady=(18, 10))
        self.progress = ttk.Progressbar(outer, mode="indeterminate")
        self.progress.pack(fill="x", pady=(0, 16))
        buttons = ttk.Frame(outer)
        buttons.pack(fill="x")
        self.start_button = ttk.Button(
            buttons, text="▶  Запустити все", style="Start.TButton", command=self.start
        )
        self.start_button.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.stop_button = ttk.Button(
            buttons, text="■  Зупинити", command=lambda: self.work(self.manager.stop)
        )
        self.stop_button.pack(side="right")
        address = ttk.Frame(outer)
        address.pack(fill="x", pady=(22, 0))
        self.url = tk.StringVar(value=self.manager.state.get("public_url", ""))
        ttk.Entry(address, textvariable=self.url, state="readonly").pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(address, text="Копіювати", command=self.copy_url).pack(side="right", padx=(8, 0))
        links = ttk.Frame(outer)
        links.pack(fill="x", pady=(14, 0))
        ttk.Button(
            links,
            text="Українізатор ↗",
            command=lambda: webbrowser.open("https://t.me/UkrainizatorBot"),
        ).pack(side="left")
        ttk.Button(
            links,
            text="Планнер ↗",
            command=lambda: webbrowser.open("https://t.me/my_telegram_task_planner_bot"),
        ).pack(side="left", padx=8)
        ttk.Button(links, text="Журнали", command=self.open_logs).pack(side="right")
        ttk.Label(
            outer,
            text="Вікно можна закрити: запущені служби залишаться у фоні.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(15, 0))
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.after(100, self.drain)
        self.window.after(500, self.start if autostart else self.refresh)
        self.window.after(10000, self.monitor)

    def work(self, action):
        if self.busy:
            return
        self.busy = True
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="disabled")
        self.progress.start(12)

        def task():
            try:
                action()
            except StartError as error:
                self.events.put({"service": "overall", "state": "error", "detail": str(error)})
            except Exception as error:
                # Never expose arbitrary exception messages (HTTP URLs may contain tokens).
                with (self.manager.data / "panel.log").open("a", encoding="utf-8") as log:
                    log.write(f"Operation failed: {type(error).__name__}\n")
                self.events.put(
                    {
                        "service": "overall",
                        "state": "error",
                        "detail": "Запуск не завершено. Перевірте журнали й натисніть кнопку повторно.",
                    }
                )
            finally:
                self.events.put({"service": "finished"})

        threading.Thread(target=task, daemon=True).start()

    def start(self):
        self.work(self.manager.start)

    def refresh(self):
        if not self.busy:
            self.work(self.manager.status)

    def monitor(self):
        self.refresh()
        self.window.after(10000, self.monitor)

    def drain(self):
        colors = {
            "ready": "#188552",
            "starting": "#d49a00",
            "stopped": "#94a3b8",
            "error": "#c43d3d",
        }
        try:
            while True:
                event = self.events.get_nowait()
                name = event["service"]
                if name == "finished":
                    self.busy = False
                    self.progress.stop()
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="normal")
                elif name == "overall":
                    self.message.set(event["detail"])
                elif name == "url":
                    self.url.set(event["detail"])
                elif name in self.rows:
                    dot, label = self.rows[name]
                    dot.configure(fg=colors[event["state"]])
                    label.configure(text=event["detail"])
        except queue.Empty:
            pass
        self.window.after(100, self.drain)

    def copy_url(self):
        if self.url.get():
            self.window.clipboard_clear()
            self.window.clipboard_append(self.url.get())

    def open_logs(self):
        os.startfile(self.manager.data)

    def close(self):
        if self.busy:
            self.window.iconify()
        else:
            self.window.destroy()


def run(autostart=False):
    lock = InstanceLock(Path(__file__).resolve().parents[1] / "data/control-panel/window.lock")
    if not lock.acquire():
        window = tk.Tk()
        window.withdraw()
        messagebox.showinfo("Панель ботів", "Панель уже відкрита. Перейдіть до її вікна.")
        window.destroy()
        return
    window = tk.Tk()
    Panel(window, autostart=autostart)
    window.mainloop()
