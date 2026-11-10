"""Headless sky logger: builds the quiet-time history even when the app is closed.

Run it in the background (e.g. Windows Task Scheduler "at log on", with pythonw.exe):
    .venv\\Scripts\\pythonw.exe planner_logger.py
It polls the public ADS-B feed once a minute (home rounded to ~1 km) and writes data/sky.sqlite.
"""
import signal
import sys
import time

from core.quiet_planner.forecast import summary_line
from core.quiet_planner.service import PlannerService


def main() -> int:
    svc = PlannerService()
    svc.start()
    signal.signal(signal.SIGINT, lambda *_: (svc.stop(), sys.exit(0)))
    print("Logging the sky every 60 s. Ctrl+C to stop.")
    while True:
        time.sleep(600)
        try:
            print(time.strftime("%H:%M"), summary_line(svc.forecast()))
        except Exception as e:
            print("forecast error:", e)


if __name__ == "__main__":
    sys.exit(main())
