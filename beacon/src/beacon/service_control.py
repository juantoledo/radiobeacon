"""The chosen audio-contention strategy: beacon actively stops/starts
SvxLink and Direwolf around each slot (CONTEXT.md's strategy #1,
"parar/arrancar por slot"), rather than relying on a host-level ALSA
dmix/dsnoop virtual device. Deploying Direwolf/SvxLink themselves (their
Dockerfiles, compose, host audio provisioning) is out of scope for this
package — this module assumes both already run as independently
start/stoppable services on the host (systemd units by default, matching
CONTEXT.md's own troubleshooting notes, which already used
`sudo systemctl stop svxlink` manually) and only controls their running
state."""
import logging
import subprocess
from typing import Protocol

logger = logging.getLogger(__name__)


class ServiceController(Protocol):
    def start(self, service_name: str) -> bool: ...
    def stop(self, service_name: str) -> bool: ...
    def is_active(self, service_name: str) -> bool: ...


class LoggingServiceController:
    """Default (BEACON_SERVICE_CONTROLLER=logging). Logs the start/stop it
    would issue instead of touching the host — the only path verifiable
    in this dev environment, same role as LoggingVoiceTransmitter."""

    def start(self, service_name: str) -> bool:
        logger.info("service_control [logging]: would start %s", service_name)
        return True

    def stop(self, service_name: str) -> bool:
        logger.info("service_control [logging]: would stop %s", service_name)
        return True

    def is_active(self, service_name: str) -> bool:
        logger.info("service_control [logging]: is_active(%s) — assuming True", service_name)
        return True


class SystemctlServiceController:
    """BEACON_SERVICE_CONTROLLER=systemctl. Shells out to
    `systemctl start|stop|is-active <service_name>` via subprocess.run.
    Never raises — logs and returns False on any non-zero exit or missing
    systemctl binary.

    Requires beacon's own process to have permission to control these
    specific units — typically a passwordless sudoers rule scoped to
    exactly `systemctl {start,stop} svxlink` and
    `systemctl {start,stop} direwolf` (NOT unrestricted sudo). This is a
    materially more privileged capability than anything else in this
    codebase, which otherwise only touches its own SQLite DB and makes
    outbound network calls — an operational/security note the host
    administrator must set up deliberately, not something that works out
    of the box. Not testable against real systemd units in a dev
    sandbox — tested only via mocking subprocess.run."""

    def _run(self, action: str, service_name: str) -> subprocess.CompletedProcess | None:
        """None means the command itself couldn't be run at all (missing
        systemctl, timeout) — distinct from a real exit code, which the
        caller interprets (a non-zero exit is a genuine failure for
        start/stop, but the *normal, expected* result for `is-active` on
        an inactive service — see is_active(), which must not log that
        as an error)."""
        try:
            return subprocess.run(["systemctl", action, service_name], capture_output=True, timeout=15)
        except FileNotFoundError:
            logger.error("service_control: systemctl not found on PATH")
            return None
        except subprocess.TimeoutExpired:
            logger.error("service_control: systemctl %s %s timed out", action, service_name)
            return None

    def start(self, service_name: str) -> bool:
        result = self._run("start", service_name)
        if result is None:
            return False
        if result.returncode != 0:
            logger.error(
                "service_control: systemctl start %s exited %d: %s",
                service_name,
                result.returncode,
                result.stderr.decode(errors="replace"),
            )
            return False
        return True

    def stop(self, service_name: str) -> bool:
        result = self._run("stop", service_name)
        if result is None:
            return False
        if result.returncode != 0:
            logger.error(
                "service_control: systemctl stop %s exited %d: %s",
                service_name,
                result.returncode,
                result.stderr.decode(errors="replace"),
            )
            return False
        return True

    def is_active(self, service_name: str) -> bool:
        # systemctl is-active exits non-zero for a simply-inactive service
        # — that's the normal, expected answer "no", not a failure worth
        # logging as an error (unlike start/stop's non-zero exit above).
        result = self._run("is-active", service_name)
        if result is None:
            return False
        return result.returncode == 0
