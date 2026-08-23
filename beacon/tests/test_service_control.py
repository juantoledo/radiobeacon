import subprocess

from beacon.service_control import LoggingServiceController, SystemctlServiceController


def test_logging_controller_start_stop_is_active_all_return_true():
    controller = LoggingServiceController()

    assert controller.start("svxlink") is True
    assert controller.stop("svxlink") is True
    assert controller.is_active("svxlink") is True


class _FakeResult:
    def __init__(self, returncode, stderr=b""):
        self.returncode = returncode
        self.stderr = stderr


def test_systemctl_start_builds_correct_command(monkeypatch):
    captured = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeResult(0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    controller = SystemctlServiceController()

    assert controller.start("svxlink") is True
    assert captured["cmd"] == ["systemctl", "start", "svxlink"]


def test_systemctl_stop_builds_correct_command(monkeypatch):
    captured = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeResult(0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    controller = SystemctlServiceController()

    assert controller.stop("direwolf") is True
    assert captured["cmd"] == ["systemctl", "stop", "direwolf"]


def test_systemctl_start_returns_false_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeResult(1, b"failed"))
    controller = SystemctlServiceController()

    assert controller.start("svxlink") is False


def test_systemctl_returns_false_when_binary_missing(monkeypatch):
    def _raise(*a, **k):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", _raise)
    controller = SystemctlServiceController()

    assert controller.start("svxlink") is False
    assert controller.stop("svxlink") is False
    assert controller.is_active("svxlink") is False


def test_systemctl_returns_false_on_timeout(monkeypatch):
    def _raise(*a, **k):
        raise subprocess.TimeoutExpired(cmd="systemctl", timeout=15)

    monkeypatch.setattr(subprocess, "run", _raise)
    controller = SystemctlServiceController()

    assert controller.start("svxlink") is False


def test_systemctl_is_active_true_on_zero_exit(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeResult(0))
    controller = SystemctlServiceController()

    assert controller.is_active("svxlink") is True


def test_systemctl_is_active_false_on_nonzero_exit_without_logging_as_error(monkeypatch, caplog):
    """systemctl is-active exits non-zero for a simply-inactive service --
    that's the normal, expected answer, not a failure worth an ERROR log
    (unlike start/stop's non-zero exit)."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeResult(3))
    controller = SystemctlServiceController()

    with caplog.at_level("ERROR"):
        result = controller.is_active("svxlink")

    assert result is False
    assert caplog.text == ""
