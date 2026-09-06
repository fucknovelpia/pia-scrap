from __future__ import annotations

import io
import json
import multiprocessing
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from tkinter import ttk, messagebox

from dotenv import dotenv_values


from src import __version__
from src.chrome_session import find_chrome_binary, list_chrome_profiles, load_chrome_novelpia_session
from src.const import APP_DIR
from src.helper import load_config, save_config

ENV_PATH = APP_DIR / ".env"
LOG_DIR = APP_DIR / "output" / "logs"
TEMP_BATCH_PREFIX = "pia-scrap-batch-"
NOVEL_PATH_RE = re.compile(r"/novel/(\d+)", re.IGNORECASE)
AUTH_ARGUMENTS = {"--user", "--pass", "--login-at", "--userkey", "--tkey"}


class QueueWriter(io.TextIOBase):
    """Stream the full log while retaining bounded recent output for the result dialog."""

    RECENT_OUTPUT_LIMIT = 65536

    def __init__(self, queue, logf):
        self._queue = queue
        self._logf = logf
        self.recent_output = ""

    def write(self, text):
        if text:
            self.recent_output = (self.recent_output + text)[-self.RECENT_OUTPUT_LIMIT:]
            self._queue.put(text)
            self._logf.write(text)
            self._logf.flush()
        return len(text) if text else 0

    def flush(self):
        pass


def build_auth_args(
    email: str, password: str, login_at: str, userkey: str, tkey: str,
) -> list[str]:
    """Use a captured browser session before saved email/password credentials."""
    login_at, userkey, tkey = login_at.strip(), userkey.strip(), tkey.strip()
    if login_at or userkey or tkey:
        values = (("--login-at", login_at), ("--userkey", userkey), ("--tkey", tkey))
    else:
        values = (("--user", email.strip()), ("--pass", password))
    return [part for flag, value in values if value for part in (flag, value)]


def redact_auth_args(args: list[str]) -> list[str]:
    """Keep authentication values out of both the live and saved command logs."""
    redacted: list[str] = []
    redact_next = False
    for arg in args:
        if redact_next:
            redacted.append("[REDACTED]")
            redact_next = False
        elif arg in AUTH_ARGUMENTS:
            redacted.append(arg)
            redact_next = True
        elif arg.split("=", 1)[0] in AUTH_ARGUMENTS and "=" in arg:
            redacted.append(arg.split("=", 1)[0] + "=[REDACTED]")
        else:
            redacted.append(arg)
    return redacted


def read_webview_login_result(path: Path) -> dict | None:
    """Return any completed login result, including cancellation or an error."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return {"status": "error", "error": "The login browser returned an invalid result."}
    return data


def parse_pasted_novel_entries(text: str) -> list[str]:
    """Return deduplicated novel IDs from pasted URLs or raw IDs."""
    entries: list[str] = []
    seen: set[str] = set()
    for raw_token in re.split(r"[\s,;]+", text or ""):
        token = raw_token.strip(" \t\r\n<>()[]{}\"'")
        if not token:
            continue
        match = NOVEL_PATH_RE.search(token)
        novel_id = match.group(1) if match else token if token.isdigit() else ""
        if novel_id and novel_id not in seen:
            seen.add(novel_id)
            entries.append(novel_id)
    return entries


def write_temporary_batch_entries(entries: list[str]) -> Path:
    """Write validated pasted IDs to a short-lived backend-compatible file."""
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".txt",
        prefix=TEMP_BATCH_PREFIX,
        delete=False,
    ) as handle:
        handle.write("\n".join(entries) + "\n")
        return Path(handle.name)


def cleanup_temporary_batch_file(path: Path) -> None:
    """Remove only paste-batch files created in the system temp directory."""
    target = Path(path).resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if target.parent != temp_root or not target.name.startswith(TEMP_BATCH_PREFIX):
        return
    try:
        target.unlink(missing_ok=True)
    except OSError:
        pass


def lock_spinbox_mouse_wheel(spinbox) -> None:
    """Prevent page scrolling from accidentally changing a spinbox value."""
    def block_mouse_wheel(_event=None):
        return "break"

    for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        spinbox.bind(sequence, block_mouse_wheel)


def launch_ui() -> None:
    # When frozen, ensure CWD is the exe's directory (not the temp extraction dir)
    if getattr(sys, 'frozen', False):
        os.chdir(Path(sys.executable).parent)

    root = tk.Tk()
    root.title(f"PIA Scrap v{__version__}")
    root.geometry("960x760")
    root.minsize(880, 680)

    cfg = load_config()
    env_cfg = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}
    chrome_profiles = list_chrome_profiles()

    def config_number(key: str, default: float) -> float:
        try:
            value = cfg.get(key, default)
            return float(default if value in (None, "") else value)
        except (TypeError, ValueError):
            return float(default)

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
    download_images_var = tk.BooleanVar(value=cfg.get("download_images", True) is not False)
    batch_links_var = tk.StringVar(value="output/novel_links.txt")
    threads_var = tk.IntVar(value=max(1, int(config_number("threads", 1))))
    legacy_interval = config_number("interval", 0.5)
    configured_min_interval = config_number("min_interval", legacy_interval)
    min_interval_var = tk.DoubleVar(value=configured_min_interval)
    max_interval_var = tk.DoubleVar(
        value=config_number("max_interval", max(2.0, configured_min_interval))
    )
    start_chapter_var = tk.IntVar(value=int(config_number("start_chapter", 0)))
    end_chapter_var = tk.IntVar(value=int(config_number("end_chapter", 0)))
    scrape_out_var = tk.StringVar(value="output/novel_links.txt")
    scrape_images_var = tk.BooleanVar(value=bool(cfg.get("scrape_images", False)))
    page_start_var = tk.StringVar(value="1")
    page_end_var = tk.StringVar(value="63")
    status_var = tk.StringVar(value="Ready.")
    busy_var = tk.BooleanVar(value=False)
    log_queue: Queue[str] = Queue()
    current_process: subprocess.Popen[str] | None = None
    auto_import_after_login = tk.BooleanVar(value=False)
    current_log_path: Path | None = None
    was_cancelled = False
    cancel_event = threading.Event()
    log_attention_generation = 0
    login_process = None
    login_result_path: Path | None = None
    login_poll_id: str | None = None

    def set_status(text: str) -> None:
        status_var.set(text)
        root.update_idletasks()

    def set_busy(is_busy: bool) -> None:
        nonlocal log_attention_generation
        busy_var.set(is_busy)
        state = "disabled" if is_busy else "normal"
        readonly_state = "disabled" if is_busy else "readonly"
        import_btn.config(state=state)
        login_btn.config(state=state)
        login_import_btn.config(state=state)
        google_login_btn.config(state=("disabled" if is_busy or login_process else "normal"))
        save_btn.config(state=state)
        save_env_btn.config(state=state)
        download_settings_btn.config(state=state)
        download_btn.config(state=state)
        batch_download_btn.config(state=state)
        paste_batch_btn.config(state=state)
        scrape_btn.config(state=state)
        profile_combo.config(state=readonly_state)
        cancel_btn.config(state=("normal" if is_busy else "disabled"))
        if is_busy:
            log_activity_var.set("Running - live output appears below")
            log_activity_bar.start(80)
        else:
            log_activity_bar.stop()
            log_activity_bar.configure(value=0)
            log_activity_var.set("Run output appears below.")
            log_attention_generation += 1
            notebook.tab(log_tab, text="Live Log")

    def append_log(text: str) -> None:
        log_text.config(state="normal")
        log_text.insert("end", text)
        log_text.see("end")
        log_text.config(state="disabled")

    def clear_log() -> None:
        log_text.config(state="normal")
        log_text.delete("1.0", "end")
        log_text.config(state="disabled")

    def animate_live_log_attention() -> None:
        """Reveal the live log and briefly pulse its tab label at run start."""
        nonlocal log_attention_generation
        log_attention_generation += 1
        generation = log_attention_generation
        frames = ("Live Log .", "Live Log ..", "Live Log ...")

        notebook.select(log_tab)

        def tick(frame_index: int = 0) -> None:
            if generation != log_attention_generation:
                return
            if frame_index >= 9:
                notebook.tab(log_tab, text="Live Log")
                return
            notebook.tab(log_tab, text=frames[frame_index % len(frames)])
            root.after(180, tick, frame_index + 1)

        tick()

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
        login_at_var.set(session.login_at or "")
        userkey_var.set(session.userkey or "")
        tkey_var.set(session.tkey or "")

        set_status(f"Imported Novelpia cookies from Chrome profile '{profile}'.")
        if not (session.login_at or session.userkey or session.tkey):
            messagebox.showwarning(
                "Chrome",
                "No usable Novelpia session was found in this Chrome profile.\n"
                "Log in first, then import again, or use Login with Google.",
            )

    def open_chrome_login(auto_import: bool = False) -> None:
        profile = profile_var.get().strip()
        if not profile:
            messagebox.showerror("Chrome", "No Chrome profile selected.")
            return

        chrome_binary = find_chrome_binary()
        if not chrome_binary:
            messagebox.showerror("Chrome", "Google Chrome could not be found on this computer.")
            return

        try:
            auto_import_after_login.set(auto_import)
            subprocess.Popen(
                [
                    chrome_binary,
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

    def read_download_controls():
        try:
            start_chapter = int(start_chapter_var.get())
            end_chapter = int(end_chapter_var.get())
            min_interval = float(min_interval_var.get())
            max_interval = float(max_interval_var.get())
            threads = int(threads_var.get())
        except (tk.TclError, TypeError, ValueError):
            messagebox.showerror("Download settings", "Chapter, thread, and interval values must be numeric.")
            return None

        if start_chapter < 0 or end_chapter < 0:
            messagebox.showerror("Chapter range", "Chapter values cannot be negative. Use 0 for no bound.")
            return None
        if start_chapter and end_chapter and start_chapter > end_chapter:
            messagebox.showerror("Chapter range", "The start chapter cannot be greater than the end chapter.")
            return None
        if min_interval < 0 or max_interval < 0:
            messagebox.showerror("Request interval", "Request intervals cannot be negative.")
            return None
        if min_interval > max_interval:
            messagebox.showerror("Request interval", "The minimum interval cannot exceed the maximum interval.")
            return None
        if threads < 1:
            messagebox.showerror("Download settings", "Threads must be at least 1.")
            return None
        return {
            "start_chapter": start_chapter,
            "end_chapter": end_chapter,
            "min_interval": min_interval,
            "max_interval": max_interval,
            "threads": threads,
        }

    def save_session_to_config() -> None:
        settings = read_download_controls()
        if settings is None:
            return
        save_config(
            {
                "login_at": login_at_var.get().strip(),
                "userkey": userkey_var.get().strip(),
                "tkey": tkey_var.get().strip(),
                "threads": settings["threads"],
                "min_interval": settings["min_interval"],
                "max_interval": settings["max_interval"],
                "start_chapter": settings["start_chapter"],
                "end_chapter": settings["end_chapter"],
                "download_images": download_images_var.get(),
                "scrape_images": scrape_images_var.get(),
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

    def run_command(
        args: list[str],
        success_message: str,
        running_message: str,
        cleanup_paths: tuple[Path, ...] = (),
    ) -> bool:
        nonlocal current_process, current_log_path
        if busy_var.get():
            return False

        def worker() -> None:
            nonlocal current_process, current_log_path
            try:
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                action = "scrape" if "--scrape-novel-links" in args else "batch-download" if "--novel-links-file" in args else "download"
                ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                current_log_path = LOG_DIR / f"{action}-{ts}.log"

                if getattr(sys, 'frozen', False):
                    # Frozen exe: run main() in-process with stdout redirected
                    with current_log_path.open("w", encoding="utf-8") as logf:
                        logf.write(f"$ PIA-Scrap.exe {' '.join(redact_auth_args(args))}\n\n")
                        writer = QueueWriter(log_queue, logf)
                        old_stdout, old_stderr = sys.stdout, sys.stderr
                        old_argv = sys.argv
                        old_cwd = os.getcwd()
                        try:
                            # CWD must be the exe's directory for .api.json, output/, etc.
                            os.chdir(Path(sys.executable).parent)
                            sys.stdout = writer
                            sys.stderr = writer
                            sys.argv = ["PIA-Scrap.exe", *args]
                            from main import main as _main
                            _main()
                            root.after(0, lambda output=writer.recent_output: finish_run(0, output, success_message))
                        except SystemExit as e:
                            code = 0 if e.code is None else e.code if isinstance(e.code, int) else 1
                            if e.code is not None and not isinstance(e.code, int):
                                writer.write(f"[error] {e.code}\n")
                            root.after(0, lambda code=code, output=writer.recent_output: finish_run(code, output, success_message))
                        except KeyboardInterrupt:
                            root.after(0, lambda output=writer.recent_output: finish_run(1, output, success_message))
                        except Exception as e:
                            import traceback
                            err = traceback.format_exc()
                            writer.write(f"\n[error] {err}\n")
                            error_message = str(e)
                            root.after(0, lambda message=error_message: finish_run(1, message, success_message))
                        finally:
                            sys.stdout = old_stdout
                            sys.stderr = old_stderr
                            sys.argv = old_argv
                            os.chdir(old_cwd)
                else:
                    # Source mode: spawn subprocess as before
                    env = dict(**__import__("os").environ)
                    env["PYTHONUNBUFFERED"] = "1"
                    env["PYTHONIOENCODING"] = "utf-8"
                    cmd = [sys.executable, "main.py", *args]
                    popen_kwargs = dict(
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL,
                        cwd=Path(__file__).resolve().parent.parent,
                        bufsize=1,
                        env=env,
                    )
                    proc = subprocess.Popen(cmd, **popen_kwargs)
                    current_process = proc
                    output_parts: list[str] = []
                    assert proc.stdout is not None
                    with current_log_path.open("w", encoding="utf-8") as logf:
                        logf.write(f"$ {' '.join(redact_auth_args(cmd))}\n\n")
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
                import traceback
                err_msg = f"[error] Failed: {e}\n{traceback.format_exc()}\n"
                log_queue.put(err_msg)
                error_message = str(e)
                root.after(0, lambda message=error_message: finish_run(1, message, success_message))
            finally:
                current_process = None
                for cleanup_path in cleanup_paths:
                    cleanup_temporary_batch_file(cleanup_path)

        set_busy(True)
        cancel_event.clear()
        try:
            from src.api import cancel_event as api_cancel
            api_cancel.clear()
        except Exception:
            pass
        set_status(running_message)
        clear_log()
        animate_live_log_attention()
        cmd_display = f"python main.py {' '.join(redact_auth_args(args))}"
        append_log(f"$ {cmd_display}\n\n")
        threading.Thread(target=worker, daemon=True).start()
        return True

    def finish_run(returncode: int, output: str, success_message: str) -> None:
        nonlocal was_cancelled
        set_busy(False)
        output = (output or "").strip()
        # Handle user cancellation (Windows terminate() returns 1, Unix returns -15)
        if was_cancelled:
            was_cancelled = False
            msg = "Download cancelled by user."
            if current_log_path:
                msg += f"\nLog: {current_log_path}"
            set_status(msg)
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
        nonlocal current_process, was_cancelled
        if was_cancelled:
            return  # already cancelling
        was_cancelled = True
        cancel_event.set()
        try:
            from src.api import cancel_event as api_cancel
            api_cancel.set()
        except Exception:
            pass
        proc = current_process
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        append_log("\n[ui] Cancel requested. Stopping...\n")
        set_status("Cancelling...")

    def run_download() -> None:
        novel_id = novel_id_var.get().strip()
        if not novel_id:
            messagebox.showerror("Download", "Please enter a novel ID.")
            return
        settings = read_download_controls()
        if settings is None:
            return

        # Auto-save settings so they persist across sessions
        save_config({
            "login_at": login_at_var.get().strip(),
            "userkey": userkey_var.get().strip(),
            "tkey": tkey_var.get().strip(),
            "threads": settings["threads"],
            "min_interval": settings["min_interval"],
            "max_interval": settings["max_interval"],
            "start_chapter": settings["start_chapter"],
            "end_chapter": settings["end_chapter"],
            "download_images": download_images_var.get(),
            "scrape_images": scrape_images_var.get(),
        })

        args = [novel_id, "--out", out_var.get().strip() or "output"]
        args += build_auth_args(
            email_var.get(), password_var.get(), login_at_var.get(),
            userkey_var.get(), tkey_var.get(),
        )
        if txt_var.get():
            args.append("--txt")
        if not download_images_var.get():
            args.append("--no-images")
        if settings["start_chapter"]:
            args += ["--start", str(settings["start_chapter"])]
        if settings["end_chapter"]:
            args += ["--end", str(settings["end_chapter"])]
        args += [
            "--threads", str(settings["threads"]),
            "--min-interval", str(settings["min_interval"]),
            "--max-interval", str(settings["max_interval"]),
        ]

        mode = "TXT" if txt_var.get() else "EPUB"
        run_command(
            args,
            f"Finished downloading novel {novel_id}.",
            f"Downloading novel {novel_id} as {mode}...",
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
        if scrape_images_var.get():
            args.append("--scrape-images")
        run_command(
            args,
            "Finished scraping novel links.",
            f"Scraping novel links from page {page_start_var.get().strip() or '1'} to {page_end_var.get().strip() or '63'}...",
        )

    def run_batch_download(
        links_file_override: str | None = None,
        cleanup_paths: tuple[Path, ...] = (),
        source_label: str | None = None,
    ) -> bool:
        settings = read_download_controls()
        if settings is None:
            return False
        save_config({
            "login_at": login_at_var.get().strip(),
            "userkey": userkey_var.get().strip(),
            "tkey": tkey_var.get().strip(),
            "threads": settings["threads"],
            "min_interval": settings["min_interval"],
            "max_interval": settings["max_interval"],
            "start_chapter": settings["start_chapter"],
            "end_chapter": settings["end_chapter"],
            "download_images": download_images_var.get(),
            "scrape_images": scrape_images_var.get(),
        })
        links_file = links_file_override or batch_links_var.get().strip() or "output/novel_links.txt"
        args = ["--novel-links-file", links_file, "--out", out_var.get().strip() or "output"]
        args += build_auth_args(
            email_var.get(), password_var.get(), login_at_var.get(),
            userkey_var.get(), tkey_var.get(),
        )
        if txt_var.get():
            args.append("--txt")
        if not download_images_var.get():
            args.append("--no-images")
        if settings["start_chapter"]:
            args += ["--start", str(settings["start_chapter"])]
        if settings["end_chapter"]:
            args += ["--end", str(settings["end_chapter"])]
        args += [
            "--threads", str(settings["threads"]),
            "--min-interval", str(settings["min_interval"]),
            "--max-interval", str(settings["max_interval"]),
        ]

        mode = "TXT" if txt_var.get() else "EPUB"
        display_source = source_label or links_file
        return run_command(
            args,
            f"Finished batch download from {display_source}.",
            f"Batch downloading novels from {display_source} as {mode}...",
            cleanup_paths=cleanup_paths,
        )

    def open_paste_batch_dialog() -> None:
        if busy_var.get():
            return

        dialog = tk.Toplevel(root)
        dialog.title("Paste URLs or Novel IDs")
        dialog.geometry("700x480")
        dialog.minsize(560, 380)
        dialog.transient(root)
        dialog.grab_set()
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(1, weight=1)

        header = ttk.Frame(dialog, padding=(18, 16, 18, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(
            header,
            text="Paste Novelpia novel URLs or numeric IDs",
            font=("TkDefaultFont", 11, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text="Use one per line, or separate entries with spaces, commas, or semicolons. Duplicates are removed.",
            wraplength=640,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))

        text_frame = ttk.Frame(dialog, padding=(18, 8))
        text_frame.grid(row=1, column=0, sticky="nsew")
        text_frame.columnconfigure(0, weight=1)
        text_frame.rowconfigure(0, weight=1)
        paste_text = tk.Text(text_frame, wrap="word", undo=True, padx=10, pady=10)
        paste_text.grid(row=0, column=0, sticky="nsew")
        paste_scroll = ttk.Scrollbar(text_frame, orient="vertical", command=paste_text.yview)
        paste_scroll.grid(row=0, column=1, sticky="ns")
        paste_text.configure(yscrollcommand=paste_scroll.set)

        footer = ttk.Frame(dialog, padding=(18, 8, 18, 16))
        footer.grid(row=2, column=0, sticky="ew")
        footer.columnconfigure(1, weight=1)
        count_var = tk.StringVar(value="0 valid novels")
        ttk.Label(footer, textvariable=count_var).grid(row=0, column=0, sticky="w")

        def refresh_count(_event=None) -> None:
            count = len(parse_pasted_novel_entries(paste_text.get("1.0", "end-1c")))
            count_var.set(f"{count} valid novel{'s' if count != 1 else ''}")

        def paste_from_clipboard() -> None:
            try:
                clipboard_text = dialog.clipboard_get()
            except tk.TclError:
                messagebox.showwarning("Clipboard", "The clipboard does not contain text.", parent=dialog)
                return
            paste_text.insert("insert", clipboard_text)
            refresh_count()

        def start_pasted_batch(_event=None) -> str:
            entries = parse_pasted_novel_entries(paste_text.get("1.0", "end-1c"))
            if not entries:
                messagebox.showerror(
                    "Paste to Batch",
                    "No valid novel URLs or numeric IDs were found.",
                    parent=dialog,
                )
                return "break"

            if read_download_controls() is None:
                return "break"
            try:
                temporary_file = write_temporary_batch_entries(entries)
            except OSError as exc:
                messagebox.showerror(
                    "Paste to Batch",
                    f"Could not prepare the pasted batch:\n\n{exc}",
                    parent=dialog,
                )
                return "break"
            dialog.grab_release()
            dialog.destroy()
            started = run_batch_download(
                links_file_override=str(temporary_file),
                cleanup_paths=(temporary_file,),
                source_label=f"{len(entries)} pasted novel{'s' if len(entries) != 1 else ''}",
            )
            if not started:
                cleanup_temporary_batch_file(temporary_file)
            return "break"

        paste_text.bind("<KeyRelease>", refresh_count)
        paste_text.bind("<Control-Return>", start_pasted_batch)
        ttk.Button(footer, text="Paste Clipboard", command=paste_from_clipboard).grid(
            row=0, column=2, padx=(8, 0)
        )
        ttk.Button(footer, text="Cancel", command=dialog.destroy).grid(
            row=0, column=3, padx=(8, 0)
        )
        ttk.Button(footer, text="Run Batch", command=start_pasted_batch).grid(
            row=0, column=4, padx=(8, 0)
        )

        paste_text.focus_set()

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

    download_tab = ttk.Frame(notebook, padding=(18, 16))
    download_tab.columnconfigure(0, weight=1)
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

    # --- Login with Google (webview) ---
    def cleanup_google_login() -> None:
        nonlocal login_process, login_result_path, login_poll_id
        if login_poll_id is not None:
            root.after_cancel(login_poll_id)
            login_poll_id = None
        proc, login_process = login_process, None
        if proc is not None:
            try:
                if proc.is_alive():
                    proc.terminate()
                proc.join(timeout=1)
                if not proc.is_alive():
                    proc.close()
            except (OSError, ValueError, AssertionError):
                pass
        if login_result_path is not None:
            try:
                login_result_path.unlink(missing_ok=True)
            except OSError:
                pass
            login_result_path = None
        google_login_btn.config(state=("disabled" if busy_var.get() else "normal"))

    def finish_google_login(data: dict) -> None:
        """Apply the child result on Tk's main thread, then release its resources."""
        cleanup_google_login()
        login_at = data.get("login_at")
        if isinstance(login_at, str) and login_at.strip():
            login_at = login_at.strip()
            userkey = data.get("userkey") or ""
            tkey = data.get("tkey") or ""
            userkey = userkey.strip() if isinstance(userkey, str) else ""
            tkey = tkey.strip() if isinstance(tkey, str) else ""
            login_at_var.set(login_at)
            userkey_var.set(userkey)
            tkey_var.set(tkey)
            login_key_var.set("")
            try:
                save_config({"login_at": login_at, "userkey": userkey, "tkey": tkey})
            except Exception as exc:
                set_status("Google login: session captured, but could not be saved.")
                messagebox.showwarning(
                    "Session not saved",
                    f"You can download using this session, but it could not be saved:\n\n{exc}",
                )
            else:
                set_status("Google login: session captured and saved successfully.")
            append_log("[auth] Google login: session captured; downloads will use this session.\n")
            return

        if data.get("status") == "cancelled":
            message = "Login browser closed before a session was captured."
        else:
            message = str(data.get("error") or "Login browser closed without detecting a session. Please try again.")
        set_status(f"Google login: {message}")
        append_log(f"[auth] {message}\n")
        if data.get("status") != "cancelled":
            messagebox.showerror("Google Login", message)

    def google_login() -> None:
        """Launch embedded webview for Google OAuth login."""
        nonlocal login_process, login_result_path, login_poll_id
        if login_process is not None or busy_var.get():
            return

        try:
            from src.webview_login import _run_webview_login

            with tempfile.NamedTemporaryFile(delete=False, suffix=".loginkey", mode="w") as tmp:
                login_result_path = Path(tmp.name)
            login_process = multiprocessing.get_context("spawn").Process(
                target=_run_webview_login, args=(str(login_result_path),), daemon=True,
            )
            login_process.start()
        except Exception as e:
            cleanup_google_login()
            messagebox.showerror("Google Login Failed", f"Could not start webview process:\n\n{e}")
            return

        deadline = time.monotonic() + 900

        def poll_result() -> None:
            nonlocal login_poll_id
            login_poll_id = None
            if login_process is None or login_result_path is None:
                return
            data = read_webview_login_result(login_result_path)
            if data is not None:
                finish_google_login(data)
                return
            if not login_process.is_alive():
                # The child may have published its result between the read and exit check.
                data = read_webview_login_result(login_result_path)
                finish_google_login(data if data is not None else {
                    "status": "error",
                    "error": "The login browser exited before detecting a session. Please try again.",
                })
                return
            if time.monotonic() >= deadline:
                finish_google_login({
                    "status": "timeout", "error": "Login was not completed within 15 minutes.",
                })
                return
            login_poll_id = root.after(250, poll_result)

        google_login_btn.config(state="disabled")
        set_status("Google login: browser opened. Log in with your Google account...")
        append_log("[auth] Opening Novelpia Global login browser...\n")
        login_poll_id = root.after(250, poll_result)

    google_login_btn = ttk.Button(creds_tab, text="Login with Google", command=google_login)
    google_login_btn.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(0, 12))

    ttk.Separator(creds_tab).grid(row=5, column=0, columnspan=3, sticky="ew", pady=(4, 12))

    ttk.Label(creds_tab, text="Chrome profile").grid(row=6, column=0, sticky="w", pady=4)
    profile_combo = ttk.Combobox(creds_tab, textvariable=profile_var, values=chrome_profiles, state="readonly")
    profile_combo.grid(row=6, column=1, sticky="ew", pady=4)
    import_btn = ttk.Button(creds_tab, text="Import From Chrome", command=import_from_chrome)
    import_btn.grid(row=6, column=2, sticky="ew", padx=(12, 0), pady=4)
    login_import_btn = ttk.Button(creds_tab, text="Login In Chrome And Import", command=open_chrome_login_and_import)
    login_import_btn.grid(row=7, column=1, sticky="ew", pady=4)
    login_btn = ttk.Button(creds_tab, text="Open Chrome Login", command=lambda: open_chrome_login(auto_import=False))
    login_btn.grid(row=7, column=2, sticky="ew", padx=(12, 0), pady=4)

    ttk.Label(creds_tab, text="login-at").grid(row=8, column=0, sticky="w", pady=4)
    ttk.Entry(creds_tab, textvariable=login_at_var).grid(row=8, column=1, columnspan=2, sticky="ew", pady=4)

    ttk.Label(creds_tab, text="USERKEY").grid(row=9, column=0, sticky="w", pady=4)
    ttk.Entry(creds_tab, textvariable=userkey_var).grid(row=9, column=1, columnspan=2, sticky="ew", pady=4)

    ttk.Label(creds_tab, text="TKEY").grid(row=10, column=0, sticky="w", pady=4)
    ttk.Entry(creds_tab, textvariable=tkey_var).grid(row=10, column=1, columnspan=2, sticky="ew", pady=4)

    ttk.Label(creds_tab, text="LOGINKEY").grid(row=11, column=0, sticky="w", pady=4)
    ttk.Entry(creds_tab, textvariable=login_key_var, state="readonly").grid(
        row=11, column=1, columnspan=2, sticky="ew", pady=4
    )
    save_btn = ttk.Button(creds_tab, text="Save Session", command=save_session_to_config)
    save_btn.grid(row=12, column=2, sticky="e", pady=(8, 0))

    ttk.Label(
        creds_tab,
        text="A captured or imported browser session is used first. Clear the session fields to use email/password.",
        wraplength=760,
        justify="left",
    ).grid(row=13, column=0, columnspan=3, sticky="w", pady=(12, 0))

    # --- Novel output -----------------------------------------------------
    novel_section = ttk.LabelFrame(download_tab, text="Novel output", padding=(14, 12))
    novel_section.grid(row=0, column=0, sticky="ew", pady=(0, 12))
    novel_section.columnconfigure(1, weight=1)

    ttk.Label(novel_section, text="Novel ID or URL", width=18).grid(
        row=0, column=0, sticky="w", padx=(0, 12), pady=6
    )
    ttk.Entry(novel_section, textvariable=novel_id_var).grid(
        row=0, column=1, columnspan=2, sticky="ew", pady=6
    )

    ttk.Label(novel_section, text="Output directory", width=18).grid(
        row=1, column=0, sticky="w", padx=(0, 12), pady=6
    )
    ttk.Entry(novel_section, textvariable=out_var).grid(
        row=1, column=1, columnspan=2, sticky="ew", pady=6
    )

    output_toggles = ttk.Frame(novel_section)
    output_toggles.grid(row=2, column=1, columnspan=2, sticky="w", pady=(6, 2))
    ttk.Checkbutton(
        output_toggles,
        text="Export TXT instead of EPUB",
        variable=txt_var,
    ).grid(row=0, column=0, sticky="w", padx=(0, 22))
    ttk.Checkbutton(
        output_toggles,
        text="Download cover and chapter images",
        variable=download_images_var,
    ).grid(row=0, column=1, sticky="w")

    # --- Batch source -----------------------------------------------------
    batch_section = ttk.LabelFrame(download_tab, text="Batch source", padding=(14, 12))
    batch_section.grid(row=1, column=0, sticky="ew", pady=(0, 12))
    batch_section.columnconfigure(1, weight=1)

    ttk.Label(batch_section, text="Links file", width=18).grid(
        row=0, column=0, sticky="w", padx=(0, 12), pady=6
    )
    ttk.Entry(batch_section, textvariable=batch_links_var).grid(
        row=0, column=1, sticky="ew", pady=6
    )
    paste_batch_btn = ttk.Button(
        batch_section,
        text="Paste to Batch...",
        command=open_paste_batch_dialog,
    )
    paste_batch_btn.grid(row=0, column=2, sticky="e", padx=(12, 0), pady=6)
    ttk.Label(
        batch_section,
        text="Choose a saved .txt list, or paste URLs and numeric IDs directly.",
        justify="left",
    ).grid(row=1, column=1, columnspan=2, sticky="w", pady=(2, 4))

    # --- Download options ------------------------------------------------
    options_section = ttk.LabelFrame(download_tab, text="Download options", padding=(14, 12))
    options_section.grid(row=2, column=0, sticky="ew", pady=(0, 12))
    options_section.columnconfigure(1, weight=1)
    options_section.columnconfigure(2, weight=1)

    ttk.Label(options_section, text="Chapter range", width=18).grid(
        row=0, column=0, sticky="w", padx=(0, 12), pady=6
    )
    chapter_range_frame = ttk.Frame(options_section)
    chapter_range_frame.grid(row=0, column=1, sticky="w", pady=6)
    ttk.Label(chapter_range_frame, text="Start").grid(row=0, column=0, padx=(0, 4))
    start_chapter_spinbox = ttk.Spinbox(
        chapter_range_frame,
        from_=0,
        to=999999,
        increment=1,
        textvariable=start_chapter_var,
        width=7,
    )
    start_chapter_spinbox.grid(row=0, column=1, padx=(0, 10))
    ttk.Label(chapter_range_frame, text="End").grid(row=0, column=2, padx=(0, 4))
    end_chapter_spinbox = ttk.Spinbox(
        chapter_range_frame,
        from_=0,
        to=999999,
        increment=1,
        textvariable=end_chapter_var,
        width=7,
    )
    end_chapter_spinbox.grid(row=0, column=3)
    ttk.Label(
        options_section,
        text="0 = first/last available chapter",
        justify="left",
    ).grid(row=0, column=2, sticky="w", padx=(18, 0), pady=6)

    ttk.Label(options_section, text="Threads", width=18).grid(
        row=1, column=0, sticky="w", padx=(0, 12), pady=6
    )
    threads_spinbox = ttk.Spinbox(
        options_section,
        from_=1,
        to=10,
        textvariable=threads_var,
        width=5,
    )
    threads_spinbox.grid(row=1, column=1, sticky="w", pady=6)
    ttk.Label(
        options_section,
        text="Concurrent download workers (1 = sequential)",
        justify="left",
    ).grid(row=1, column=2, sticky="w", padx=(18, 0), pady=6)

    ttk.Label(options_section, text="Interval range (s)", width=18).grid(
        row=2, column=0, sticky="w", padx=(0, 12), pady=6
    )
    interval_range_frame = ttk.Frame(options_section)
    interval_range_frame.grid(row=2, column=1, sticky="w", pady=6)
    ttk.Label(interval_range_frame, text="Min").grid(row=0, column=0, padx=(0, 4))
    min_interval_spinbox = ttk.Spinbox(
        interval_range_frame,
        from_=0.0,
        to=60.0,
        increment=0.1,
        textvariable=min_interval_var,
        width=7,
        format="%.1f",
    )
    min_interval_spinbox.grid(row=0, column=1, padx=(0, 10))
    ttk.Label(interval_range_frame, text="Max").grid(row=0, column=2, padx=(0, 4))
    max_interval_spinbox = ttk.Spinbox(
        interval_range_frame,
        from_=0.0,
        to=60.0,
        increment=0.1,
        textvariable=max_interval_var,
        width=7,
        format="%.1f",
    )
    max_interval_spinbox.grid(row=0, column=3)
    ttk.Label(
        options_section,
        text="Fresh random delay before each worker's chapter request",
        justify="left",
    ).grid(row=2, column=2, sticky="w", padx=(18, 0), pady=6)

    for spinbox in (
        start_chapter_spinbox,
        end_chapter_spinbox,
        threads_spinbox,
        min_interval_spinbox,
        max_interval_spinbox,
    ):
        lock_spinbox_mouse_wheel(spinbox)

    # --- Actions ----------------------------------------------------------
    actions = ttk.Frame(download_tab)
    actions.grid(row=3, column=0, sticky="ew", pady=(2, 0))
    actions.columnconfigure(1, weight=1)

    download_settings_btn = ttk.Button(
        actions,
        text="Save Settings",
        command=save_session_to_config,
    )
    download_settings_btn.grid(row=0, column=0, sticky="w")

    cancel_btn = ttk.Button(actions, text="Cancel", command=cancel_run, state="disabled", width=12)
    cancel_btn.grid(row=0, column=2, sticky="e", padx=(8, 0))
    batch_download_btn = ttk.Button(actions, text="Run File Batch", command=run_batch_download, width=16)
    batch_download_btn.grid(row=0, column=3, sticky="e", padx=(8, 0))
    download_btn = ttk.Button(actions, text="Download Novel", command=run_download, width=16)
    download_btn.grid(row=0, column=4, sticky="e", padx=(8, 0))

    ttk.Label(scrape_tab, text="Page start").grid(row=0, column=0, sticky="w", pady=4)
    ttk.Entry(scrape_tab, textvariable=page_start_var).grid(row=0, column=1, sticky="ew", pady=4)

    ttk.Label(scrape_tab, text="Page end").grid(row=1, column=0, sticky="w", pady=4)
    ttk.Entry(scrape_tab, textvariable=page_end_var).grid(row=1, column=1, sticky="ew", pady=4)

    ttk.Label(scrape_tab, text="Links output").grid(row=2, column=0, sticky="w", pady=4)
    ttk.Entry(scrape_tab, textvariable=scrape_out_var).grid(row=2, column=1, columnspan=2, sticky="ew", pady=4)

    ttk.Checkbutton(
        scrape_tab,
        text="Download listing cover images",
        variable=scrape_images_var,
    ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))

    scrape_btn = ttk.Button(scrape_tab, text="Run Link Scrape", command=run_link_scrape)
    scrape_btn.grid(row=4, column=2, sticky="e", pady=(12, 0))

    log_header = ttk.Frame(log_tab)
    log_header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
    log_header.columnconfigure(1, weight=1)
    ttk.Label(log_header, text="Live Log").grid(row=0, column=0, sticky="w")
    log_activity_var = tk.StringVar(value="Run output appears below.")
    ttk.Label(log_header, textvariable=log_activity_var).grid(
        row=0, column=1, sticky="e", padx=(16, 10)
    )
    log_activity_bar = ttk.Progressbar(
        log_header,
        mode="indeterminate",
        length=150,
    )
    log_activity_bar.grid(row=0, column=2, sticky="e")
    log_text = tk.Text(log_tab, height=18, wrap="word", state="disabled")
    log_text.grid(row=1, column=0, sticky="nsew")
    log_scroll = ttk.Scrollbar(log_tab, orient="vertical", command=log_text.yview)
    log_scroll.grid(row=1, column=1, sticky="ns")
    log_text.configure(yscrollcommand=log_scroll.set)

    def on_close():
        """Clean shutdown: cancel any running work, then force-exit."""
        cleanup_google_login()
        cancel_event.set()
        try:
            from src.api import cancel_event as api_cancel
            api_cancel.set()
        except Exception:
            pass
        root.destroy()
        # Force exit to prevent PyInstaller temp dir cleanup warnings
        import os
        os._exit(0)

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.after(120, poll_log_queue)
    root.bind("<FocusIn>", on_focus_in)

    root.mainloop()
