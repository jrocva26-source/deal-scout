"""
Deal Scout Watchdog - Keeps the bot alive 24/7.

Launches deal_scout.py as a subprocess and restarts it if it crashes.
Designed to be run by Windows Task Scheduler on startup.

Usage:
    pythonw.exe watchdog.py          # Silent (no console window)
    python watchdog.py               # With console output for debugging
"""

import subprocess
import sys
import os
import time
import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Always run from the script's directory
os.chdir(Path(__file__).parent)

# Set UTF-8 mode
os.environ["PYTHONUTF8"] = "1"

# Logging
log = logging.getLogger("watchdog")
log.setLevel(logging.INFO)

handler = RotatingFileHandler(
    "watchdog.log", maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8"
)
handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(handler)

# Also log to console if running with python.exe (not pythonw.exe)
if sys.executable.lower().endswith("python.exe"):
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(console)

# Config
PYTHON = str(Path(__file__).parent / "venv" / "Scripts" / "python.exe")
SCRIPT = str(Path(__file__).parent / "deal_scout.py")
MIN_RESTART_DELAY = 10       # Seconds before restarting after crash
MAX_RESTART_DELAY = 300      # Max backoff (5 minutes)
RAPID_CRASH_WINDOW = 60      # If it crashes within this many seconds, increase delay
MAX_RAPID_CRASHES = 5        # After this many rapid crashes, wait longer


def run_bot():
    """Run the bot in a loop, restarting on crashes."""
    restart_delay = MIN_RESTART_DELAY
    rapid_crashes = 0

    log.info("=" * 50)
    log.info("Deal Scout Watchdog started")
    log.info(f"Python: {PYTHON}")
    log.info(f"Script: {SCRIPT}")
    log.info("=" * 50)

    while True:
        start_time = time.time()
        log.info("Starting Deal Scout...")

        try:
            process = subprocess.Popen(
                [PYTHON, SCRIPT],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(Path(__file__).parent),
                env={**os.environ, "PYTHONUTF8": "1"},
            )

            log.info(f"Bot started (PID {process.pid})")

            # Stream output to log
            for line in process.stdout:
                try:
                    text = line.decode("utf-8", errors="replace").rstrip()
                    if text:
                        log.info(f"[bot] {text}")
                except Exception:
                    pass

            process.wait()
            exit_code = process.returncode
            runtime = time.time() - start_time

            log.warning(
                f"Bot exited with code {exit_code} after "
                f"{runtime:.0f}s ({runtime/3600:.1f}h)"
            )

        except Exception as e:
            runtime = time.time() - start_time
            log.error(f"Failed to run bot: {e}")

        # Backoff logic
        if runtime < RAPID_CRASH_WINDOW:
            rapid_crashes += 1
            log.warning(f"Rapid crash #{rapid_crashes} (ran only {runtime:.0f}s)")

            if rapid_crashes >= MAX_RAPID_CRASHES:
                restart_delay = min(restart_delay * 2, MAX_RESTART_DELAY)
                log.warning(
                    f"Too many rapid crashes — backing off to {restart_delay}s"
                )
        else:
            # Ran for a while, reset backoff
            rapid_crashes = 0
            restart_delay = MIN_RESTART_DELAY

        log.info(f"Restarting in {restart_delay} seconds...")
        time.sleep(restart_delay)


if __name__ == "__main__":
    try:
        run_bot()
    except KeyboardInterrupt:
        log.info("Watchdog stopped by user")
    except Exception as e:
        log.error(f"Watchdog crashed: {e}")
        raise
