"""Cron-style scheduler plus manual triggers (web button, hassio.addon_stdin).

Every run goes through this loop, which also keeps sensor.spotify_backup_status
up to date in Home Assistant.
"""
import logging
import sys
import threading
from datetime import datetime

from croniter import croniter

from . import ha, jobs
from .config import load_options

log = logging.getLogger(__name__)

STATUS_INTERVAL = 300  # seconds between sensor refreshes (states vanish when HA restarts)


class Scheduler:
    def __init__(self, cron: str):
        if not croniter.is_valid(cron):
            raise ValueError(f"Invalid schedule {cron!r}. Use cron syntax, e.g. '0 3 * * *' (daily 3am) "
                             f"or '0 3 * * 1' (Mondays 3am).")
        self.cron = cron
        self.next_run: datetime | None = None
        self._wake = threading.Event()
        self._pending_trigger: str | None = None
        self.heartbeat: datetime | None = None  # last time the loop ticked; used by /health

    def healthy(self) -> bool:
        if jobs.is_running():
            return True
        return self.heartbeat is not None and \
            (datetime.now().astimezone() - self.heartbeat).total_seconds() < 3 * STATUS_INTERVAL

    def trigger(self, source: str):
        self._pending_trigger = source
        self._wake.set()

    def _compute_next(self):
        self.next_run = croniter(self.cron, datetime.now().astimezone()).get_next(datetime)

    def publish_status(self, running: bool = False):
        try:
            ha.publish_status(jobs.open_db(load_options()), self.next_run, running)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not publish status: %s", e)

    def _run(self, trigger: str):
        self.publish_status(running=True)
        try:
            jobs.run_job(trigger)
        finally:
            self.publish_status()

    def loop(self):
        self._compute_next()
        log.info("Schedule '%s' - next run %s", self.cron, self.next_run.strftime("%Y-%m-%d %H:%M %Z"))
        self.publish_status()
        while True:
            self.heartbeat = datetime.now().astimezone()
            timeout =max(0.0, (self.next_run - datetime.now().astimezone()).total_seconds())
            if self._wake.wait(timeout=min(timeout, STATUS_INTERVAL)):
                self._wake.clear()
                self._run(self._pending_trigger or "manual")
            elif datetime.now().astimezone() >= self.next_run:
                self._compute_next()  # before the run, so the sensor shows the following run
                self._run("schedule")
                log.info("Next run %s", self.next_run.strftime("%Y-%m-%d %H:%M %Z"))
            else:
                self.publish_status()

    def listen_stdin(self):
        """Lets HA automations trigger a run: service hassio.addon_stdin, input: "run"."""
        for line in sys.stdin:
            cmd = line.strip().strip('"').lower()
            if cmd in ("run", "run_now", "backup"):
                log.info("Run requested via stdin")
                self.trigger("home_assistant")
            elif cmd:
                log.warning("Unknown stdin command %r (expected 'run')", cmd)
