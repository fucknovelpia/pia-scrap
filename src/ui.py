from __future__ import annotations

import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from tkinter import ttk, messagebox

from dotenv import dotenv_values

from src.api import FETCH_PROFILES
from src.chrome_session import list_chrome_profiles, load_chrome_novelpia_session
from src.helper import load_config, save_config

CHROME_BINARY = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
LOG_DIR = Path(__file__).resolve().parent.parent / "output" / "logs"


def launch_ui() -> None:
    root = tk.Tk()
    root.title("PIA Scrap")
    root.geometry("900x700")
    root.minsize(840, 620)

    cfg = load_config()
    env_cfg = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}
    chrome_profiles = list_chrome_profiles()

    profile_var = tk.StringVar(value=(chrome_profiles[0] if chrome_profiles else ""))
    email_var = tk.StringVar(value=str(env_cfg.get("NOVELPIA_EMAIL") or ""))
    password_var = tk.StringVar(value=str(env_cfg.get("NOVELPIA_PASSWORD") or ""))
    login_at_var = tk.StringVar(value=(cfg.get("login_at") or ""))
    userkey_var = tk.StringVar(value=(cfg.get("userkey") or ""))
    tkey_var = tk.StringVar(value=(cfg.get("tkey") or ""))
    login_key_var = tk.StringVar(value="")

    novel_id_var = tk.StringVar()
    out_var = tk.StringVar(value="output")
    txt_var = tk.BooleanVar(value=False)
    batch_links_var = tk.StringVar(value="output/novel_links.txt")
    saved_fetch_profile = str(cfg.get("fetch_profile") or "safe")
    if saved_fetch_profile not in FETCH_PROFILES:
        saved_fetch_profile = "safe"
    fetch_profile_var = tk.StringVar(value=saved_fetch_profile)
    scrape_out_var = tk.StringVar(value="output/novel_links.txt")
    page_start_var = tk.StringVar(value="1")
    page_end_var = tk.StringVar(value="63")
    status_var = tk.StringVar(value="Ready.")
    busy_var = tk.BooleanVar(value=False)
    log_queue: Queue[str] = Queue()
    current_process: subprocess.Popen[str] | None = None
    auto_import_after_login = tk.BooleanVar(value=False)
    current_log_path: Path | None = None

    def set_status(text: str) -> None:
        status_var.set(text)
        root.update_idletasks()

    def set_busy(is_busy: bool) -> None:
        busy_var.set(is_busy)
        state = "disabled" if is_busy else "normal"
        readonly_state = "disabled" if is_busy else "readonly"
        import_btn.config(state=state)
        login_btn.config(state=state)
        login_import_btn.config(state=state)
        save_btn.config(state=state)
        save_env_btn.config(state=state)
        download_btn.config(state=state)
        batch_download_btn.config(state=state)
        scrape_btn.config(state=state)
        profile_combo.config(state=readonly_state)
        profile_combo_download.config(state=readonly_state)
        cancel_btn.config(state=("normal" if is_busy else "disabled"))

    def append_log(text: str) -> None:
        log_text.config(state="normal")
        log_text.insert("end", text)
        log_text.see("end")
        log_text.config(state="disabled")

    def clear_log() -> None:
        log_text.config(state="normal")
        log_text.delete("1.0", "end")
        log_text.config(state="disabled")

    def summarize_output(output: str, fallback: str) -> str:
        lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
        for line in reversed(lines):
            if line.startswith("[success]"):
                return line
        for line in reversed(lines):
            if line.startswith("[error]"):
                return line
        if lines:
            return lines[-1][:500]
        return fallback

    def poll_log_queue() -> None:
        try:
            while True:
                append_log(log_queue.get_nowait())
        except Empty:
            pass
        root.after(120, poll_log_queue)

    def import_from_chrome() -> None:
        profile = profile_var.get().strip()
        if not profile:
            messagebox.showerror("Chrome", "No Chrome profile selected.")
            return
        try:
            session = load_chrome_novelpia_session(profile)
        except Exception as e:
            messagebox.showerror("Chrome", f"Could not read Novelpia session from Chrome:\n\n{e}")
            return

        login_key_var.set(session.login_key or "")
        if session.login_at:
            login_at_var.set(session.login_at)
        if session.userkey:
            userkey_var.set(session.userkey)
        if session.tkey:
            tkey_var.set(session.tkey)

        set_status(f"Imported Novelpia cookies from Chrome profile '{profile}'.")
        if not session.login_key:
            messagebox.showwarning(
                "Chrome",
                "Chrome cookies were imported, but LOGINKEY was not found.\n"
                "You may still need to paste a valid login-at token manually.",
            )

    def open_chrome_login(auto_import: bool = False) -> None:
        profile = profile_var.get().strip()
        if not profile:
            messagebox.showerror("Chrome", "No Chrome profile selected.")
            return

        chrome_path = Path(CHROME_BINARY)
        if not chrome_path.exists():
            messagebox.showerror("Chrome", f"Chrome binary not found at:\n\n{CHROME_BINARY}")
            return

        try:
            auto_import_after_login.set(auto_import)
            subprocess.Popen(
                [
                    CHROME_BINARY,
                    f"--profile-directory={profile}",
                    "--new-window",
                    "https://global.novelpia.com/login",
                ],
                cwd=Path(__file__).resolve().parent.parent,
            )
            if auto_import:
                set_status(
                    f"Opened Chrome login for '{profile}'. After you log in, come back to this window and it will auto-import."
                )
                append_log(
                    f"[ui] Opened Chrome login for profile '{profile}'. Auto-import is armed for your return.\n"
                )
            else:
                set_status(f"Opened Chrome login window for profile '{profile}'. Log in there, then click Import From Chrome.")
                append_log(f"[ui] Opened Chrome for profile '{profile}' at Novelpia login.\n")
        except Exception as e:
            messagebox.showerror("Chrome", f"Could not open Chrome:\n\n{e}")

    def open_chrome_login_and_import() -> None:
        open_chrome_login(auto_import=True)

    def on_focus_in(_event=None) -> None:
        if not auto_import_after_login.get():
            return
        auto_import_after_login.set(False)
        append_log("[ui] UI focus restored. Attempting automatic Chrome import...\n")
        import_from_chrome()

    def save_session_to_config() -> None:
        save_config(
            {
                "login_at": login_at_var.get().strip(),
                "userkey": userkey_var.get().strip(),
                "tkey": tkey_var.get().strip(),
                "fetch_profile": fetch_profile_var.get().strip() or "safe",
            }
        )
        set_status("Saved session to .api.json.")
        messagebox.showinfo("Saved", "Session saved to .api.json")

    def save_credentials_to_env() -> None:
        email = email_var.get().strip()
        password = password_var.get().strip()
        try:
            lines = [
                "# Novelpia Credentials",
                f"NOVELPIA_EMAIL={email}",
                f'NOVELPIA_PASSWORD={password}',
                "",
            ]
            ENV_PATH.write_text("\n".join(lines), encoding="utf-8")
        except Exception as e:
            messagebox.showerror("Save failed", f"Could not write .env:\n\n{e}")
            return

        set_status("Saved credentials to .env.")
        messagebox.showinfo("Saved", "Credentials saved to .env")

    def run_command(args: list[str], success_message: str, running_message: str) -> None:
        nonlocal current_process, current_log_path
        if busy_var.get():
            return

        def worker() -> None:
            nonlocal current_process, current_log_path
            try:
                env = dict(**__import__("os").environ)
                env["PYTHONUNBUFFERED"] = "1"
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                action = "scrape" if "--scrape-novel-links" in args else "batch-download" if "--novel-links-file" in args else "download"
                ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                current_log_path = LOG_DIR / f"{action}-{ts}.log"
                proc = subprocess.Popen(
                    [sys.executable, "main.py", *args],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=Path(__file__).resolve().parent.parent,
                    bufsize=1,
                    env=env,
                )
                current_process = proc
                output_parts: list[str] = []
                assert proc.stdout is not None
                with current_log_path.open("w", encoding="utf-8") as logf:
                    logf.write(f"$ {' '.join(['python', 'main.py', *args])}\n\n")
                    for raw_line in proc.stdout:
                        line = raw_line.replace("\r", "\n")
                        output_parts.append(line)
                        logf.write(line)
                        logf.flush()
                        log_queue.put(line)
                proc.wait()
                output = "".join(output_parts)
                root.after(0, lambda: finish_run(proc.returncode or 0, output, success_message))
            except Exception as e:
                root.after(0, lambda: finish_run(1, str(e), success_message))
            finally:
                current_process = None

        set_busy(True)
        set_status(running_message)
        clear_log()
        append_log(f"$ {' '.join(['python', 'main.py', *args])}\n\n")
        threading.Thread(target=worker, daemon=True).start()

    def finish_run(returncode: int, output: str, success_message: str) -> None:
        set_busy(False)
        output = (output or "").strip()
        if returncode == -15:
            msg = "Command cancelled."
            if current_log_path:
                msg += f" Log: {current_log_path}"
            set_status(msg)
            messagebox.showinfo("Cancelled", msg)
            return
        if returncode == 0:
            msg = summarize_output(output, success_message)
            if current_log_path:
                msg += f"\n\nLog saved to:\n{current_log_path}"
            set_status(success_message if not current_log_path else f"{success_message} Log: {current_log_path}")
            messagebox.showinfo("Success", msg)
        else:
            msg = summarize_output(output, f"Exit code {returncode}")
            if current_log_path:
                msg += f"\n\nFull log saved to:\n{current_log_path}"
            set_status("Command failed." if not current_log_path else f"Command failed. Log: {current_log_path}")
            messagebox.showerror("Run failed", msg)

    def cancel_run() -> None:
        nonlocal current_process
        proc = current_process
        if not proc or proc.poll() is not None:
            set_status("No running command to cancel.")
            return
        try:
            proc.terminate()
            append_log("\n[ui] Cancel requested. Terminating running command...\n")
            set_status("Cancelling command...")
        except Exception as e:
            messagebox.showerror("Cancel failed", str(e))

    def run_download() -> None:
        novel_id = novel_id_var.get().strip()
        if not novel_id:
            messagebox.showerror("Download", "Please enter a novel ID.")
            return

        args = [novel_id, "--out", out_var.get().strip() or "output"]
        if email_var.get().strip():
            args += ["--user", email_var.get().strip()]
        if password_var.get().strip():
            args += ["--pass", password_var.get().strip()]
        if login_at_var.get().strip():
            args += ["--login-at", login_at_var.get().strip()]
        if userkey_var.get().strip():
            args += ["--userkey", userkey_var.get().strip()]
        if tkey_var.get().strip():
            args += ["--tkey", tkey_var.get().strip()]
        if txt_var.get():
            args.append("--txt")
        args += ["--fetch-profile", fetch_profile_var.get().strip() or "safe"]

        mode = "TXT" if txt_var.get() else "EPUB"
        profile_label = fetch_profile_var.get().strip() or "safe"
        run_command(
            args,
            f"Finished downloading novel {novel_id}.",
            f"Downloading novel {novel_id} as {mode} with profile {profile_label}...",
        )

    def run_link_scrape() -> None:
        args = [
            "--scrape-novel-links",
            "--page-start",
            page_start_var.get().strip() or "1",
            "--page-end",
            page_end_var.get().strip() or "63",
            "--links-out",
            scrape_out_var.get().strip() or "output/novel_links.txt",
        ]
        run_command(
            args,
            "Finished scraping novel links.",
            f"Scraping novel links from page {page_start_var.get().strip() or '1'} to {page_end_var.get().strip() or '63'}...",
        )

    def run_batch_download() -> None:
        links_file = batch_links_var.get().strip() or "output/novel_links.txt"
        args = ["--novel-links-file", links_file, "--out", out_var.get().strip() or "output"]
        if email_var.get().strip():
            args += ["--user", email_var.get().strip()]
        if password_var.get().strip():
            args += ["--pass", password_var.get().strip()]
        if login_at_var.get().strip():
            args += ["--login-at", login_at_var.get().strip()]
        if userkey_var.get().strip():
            args += ["--userkey", userkey_var.get().strip()]
        if tkey_var.get().strip():
            args += ["--tkey", tkey_var.get().strip()]
        if txt_var.get():
            args.append("--txt")
        args += ["--fetch-profile", fetch_profile_var.get().strip() or "safe"]

        mode = "TXT" if txt_var.get() else "EPUB"
        profile_label = fetch_profile_var.get().strip() or "safe"
        run_command(
            args,
            f"Finished batch download from {links_file}.",
            f"Batch downloading novels from {links_file} as {mode} with profile {profile_label}...",
        )

    root.columnconfigure(0, weight=1)
    root.rowconfigure(1, weight=1)

    status_bar = ttk.Frame(root, padding=(12, 10))
    status_bar.grid(row=0, column=0, sticky="ew")
    status_bar.columnconfigure(0, weight=1)
    ttk.Label(status_bar, textvariable=status_var).grid(row=0, column=0, sticky="w")

    notebook = ttk.Notebook(root)
    notebook.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))

    creds_tab = ttk.Frame(notebook, padding=16)
    creds_tab.columnconfigure(1, weight=1)
    creds_tab.columnconfigure(2, weight=1)
    notebook.add(creds_tab, text="Login")

    download_tab = ttk.Frame(notebook, padding=16)
    download_tab.columnconfigure(1, weight=1)
    download_tab.columnconfigure(2, weight=1)
    notebook.add(download_tab, text="Download")

    scrape_tab = ttk.Frame(notebook, padding=16)
    scrape_tab.columnconfigure(1, weight=1)
    scrape_tab.columnconfigure(2, weight=1)
    notebook.add(scrape_tab, text="Scrape")

    log_tab = ttk.Frame(notebook, padding=16)
    log_tab.columnconfigure(0, weight=1)
    log_tab.rowconfigure(1, weight=1)
    notebook.add(log_tab, text="Live Log")

    ttk.Label(creds_tab, text="Email").grid(row=0, column=0, sticky="w", pady=4)
    ttk.Entry(creds_tab, textvariable=email_var).grid(row=0, column=1, columnspan=2, sticky="ew", pady=4)

    ttk.Label(creds_tab, text="Password").grid(row=1, column=0, sticky="w", pady=4)
    ttk.Entry(creds_tab, textvariable=password_var, show="*").grid(row=1, column=1, columnspan=2, sticky="ew", pady=4)
    save_env_btn = ttk.Button(creds_tab, text="Save Credentials", command=save_credentials_to_env)
    save_env_btn.grid(row=2, column=2, sticky="e", pady=(4, 12))

    ttk.Separator(creds_tab).grid(row=3, column=0, columnspan=3, sticky="ew", pady=(4, 12))

    ttk.Label(creds_tab, text="Chrome profile").grid(row=4, column=0, sticky="w", pady=4)
    profile_combo = ttk.Combobox(creds_tab, textvariable=profile_var, values=chrome_profiles, state="readonly")
    profile_combo.grid(row=4, column=1, sticky="ew", pady=4)
    import_btn = ttk.Button(creds_tab, text="Import From Chrome", command=import_from_chrome)
    import_btn.grid(row=4, column=2, sticky="ew", padx=(12, 0), pady=4)
    login_import_btn = ttk.Button(creds_tab, text="Login In Chrome And Import", command=open_chrome_login_and_import)
    login_import_btn.grid(row=5, column=1, sticky="ew", pady=4)
    login_btn = ttk.Button(creds_tab, text="Open Chrome Login", command=lambda: open_chrome_login(auto_import=False))
    login_btn.grid(row=5, column=2, sticky="ew", padx=(12, 0), pady=4)

    ttk.Label(creds_tab, text="login-at").grid(row=6, column=0, sticky="w", pady=4)
    ttk.Entry(creds_tab, textvariable=login_at_var).grid(row=6, column=1, columnspan=2, sticky="ew", pady=4)

    ttk.Label(creds_tab, text="USERKEY").grid(row=7, column=0, sticky="w", pady=4)
    ttk.Entry(creds_tab, textvariable=userkey_var).grid(row=7, column=1, columnspan=2, sticky="ew", pady=4)

    ttk.Label(creds_tab, text="TKEY").grid(row=8, column=0, sticky="w", pady=4)
    ttk.Entry(creds_tab, textvariable=tkey_var).grid(row=8, column=1, columnspan=2, sticky="ew", pady=4)

    ttk.Label(creds_tab, text="LOGINKEY").grid(row=9, column=0, sticky="w", pady=4)
    ttk.Entry(creds_tab, textvariable=login_key_var, state="readonly").grid(
        row=9, column=1, columnspan=2, sticky="ew", pady=4
    )
    save_btn = ttk.Button(creds_tab, text="Save Session", command=save_session_to_config)
    save_btn.grid(row=10, column=2, sticky="e", pady=(8, 0))

    ttk.Label(
        creds_tab,
        text="Email/password are used first. Imported browser session is optional.",
        wraplength=760,
        justify="left",
    ).grid(row=11, column=0, columnspan=3, sticky="w", pady=(12, 0))

    ttk.Label(download_tab, text="Novel ID").grid(row=0, column=0, sticky="w", pady=4)
    ttk.Entry(download_tab, textvariable=novel_id_var).grid(row=0, column=1, sticky="ew", pady=4)
    ttk.Checkbutton(download_tab, text="Export TXT instead of EPUB", variable=txt_var).grid(
        row=0, column=2, sticky="w", padx=(12, 0), pady=4
    )

    ttk.Label(download_tab, text="Output dir").grid(row=1, column=0, sticky="w", pady=4)
    ttk.Entry(download_tab, textvariable=out_var).grid(row=1, column=1, columnspan=2, sticky="ew", pady=4)

    ttk.Label(download_tab, text="Batch links file").grid(row=2, column=0, sticky="w", pady=4)
    ttk.Entry(download_tab, textvariable=batch_links_var).grid(row=2, column=1, columnspan=2, sticky="ew", pady=4)

    ttk.Label(download_tab, text="Speed profile").grid(row=3, column=0, sticky="w", pady=4)
    profile_values = list(sorted(FETCH_PROFILES.keys()))
    profile_combo_download = ttk.Combobox(
        download_tab,
        textvariable=fetch_profile_var,
        values=profile_values,
        state="readonly",
    )
    profile_combo_download.grid(row=3, column=1, sticky="ew", pady=4)
    ttk.Label(
        download_tab,
        text="safe = current conservative mode | fast-rotate = original-style speed with session refresh/re-login on failure",
        wraplength=420,
        justify="left",
    ).grid(row=3, column=2, sticky="w", padx=(12, 0), pady=4)
    ttk.Button(
        download_tab,
        text="Save Profile",
        command=save_session_to_config,
    ).grid(row=4, column=0, sticky="w", pady=(12, 0))

    cancel_btn = ttk.Button(download_tab, text="Cancel", command=cancel_run, state="disabled")
    cancel_btn.grid(row=4, column=1, sticky="e", pady=(12, 0))
    batch_download_btn = ttk.Button(download_tab, text="Run Batch Download", command=run_batch_download)
    batch_download_btn.grid(row=4, column=2, sticky="w", pady=(12, 0))
    download_btn = ttk.Button(download_tab, text="Run Download", command=run_download)
    download_btn.grid(row=4, column=2, sticky="e", pady=(12, 0))

    ttk.Label(scrape_tab, text="Page start").grid(row=0, column=0, sticky="w", pady=4)
    ttk.Entry(scrape_tab, textvariable=page_start_var).grid(row=0, column=1, sticky="ew", pady=4)

    ttk.Label(scrape_tab, text="Page end").grid(row=1, column=0, sticky="w", pady=4)
    ttk.Entry(scrape_tab, textvariable=page_end_var).grid(row=1, column=1, sticky="ew", pady=4)

    ttk.Label(scrape_tab, text="Links output").grid(row=2, column=0, sticky="w", pady=4)
    ttk.Entry(scrape_tab, textvariable=scrape_out_var).grid(row=2, column=1, columnspan=2, sticky="ew", pady=4)

    scrape_btn = ttk.Button(scrape_tab, text="Run Link Scrape", command=run_link_scrape)
    scrape_btn.grid(row=3, column=2, sticky="e", pady=(12, 0))

    ttk.Label(log_tab, text="Live Log").grid(row=0, column=0, sticky="w", pady=(0, 6))
    log_text = tk.Text(log_tab, height=18, wrap="word", state="disabled")
    log_text.grid(row=1, column=0, sticky="nsew")
    log_scroll = ttk.Scrollbar(log_tab, orient="vertical", command=log_text.yview)
    log_scroll.grid(row=1, column=1, sticky="ns")
    log_text.configure(yscrollcommand=log_scroll.set)

    root.after(120, poll_log_queue)
    root.bind("<FocusIn>", on_focus_in)

    root.mainloop()
