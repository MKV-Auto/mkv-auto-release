"""MakeMKV finding zero drives must say why, not just fail (#802).

A user reported "the container doesn't detect my disc". The engine had in fact
never seen a *drive*: every ``makemkvcon`` run returned ``MSG:5042`` with all
sixteen DRV slots empty, then ``MSG:2024 "Unknown device - '/dev/sr0'"``, while
udev fired normally for both ``/dev/sr0`` and ``/dev/sr1``.

MakeMKV enumerates optical drives through SCSI generic. With no ``/dev/sg*`` it
finds nothing — the host is missing the ``sg`` kernel module, which a container
cannot supply because it shares the host kernel. The old behaviour rendered
this fully-diagnosable state as a bare "Disc not found".

The fixture is the reported log, trimmed to one invocation and otherwise
verbatim, so these tests fail if the signature we key on ever stops matching
real output.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core import drive_registry
from core.utils import (
    MakeMKVError,
    MakeMKVNoDrivesError,
    diagnose_makemkv_no_drives,
)

FIXTURE = Path(__file__).parent / "fixtures" / "makemkvcon_no_usable_drives.log"

HEALTHY_OUTPUT = (
    'MSG:1005,0,1,"MakeMKV v1.18.4 linux(x64-release) started","%1 started","x"\n'
    'DRV:0,2,999,12,"BD-RE PIONEER BD-RW   BDR-XD06U","MOVIE_DISC","/dev/sr0"\n'
    'TCOUNT:12\n'
)


def _fake_env(monkeypatch, sr, sg, accessible=True, host_sg=None):
    """Pretend the container sees ``sr``/``sg`` and the host kernel has ``host_sg``.

    ``host_sg`` defaults to mirroring ``sg`` — the ordinary case where whatever
    the container has is what the host has. Pass it explicitly to model the
    split that bit two reporters: the host has SCSI generic devices and the
    container was given none.

    ``os.access`` must be patched too: the fake paths do not exist, so the real
    call would report every node inaccessible and every scenario would collapse
    into the permission branch.
    """
    if host_sg is None:
        host_sg = [p.rsplit("/", 1)[-1] for p in sg]
    monkeypatch.setattr(drive_registry, "_enumerate_devices", lambda: list(sr))
    monkeypatch.setattr(drive_registry, "_enumerate_scsi_generic", lambda: list(sg))
    monkeypatch.setattr(drive_registry, "_enumerate_host_scsi_generic", lambda: list(host_sg))
    monkeypatch.setattr(drive_registry.os, "access", lambda *a, **k: accessible)


@pytest.fixture
def no_sg_host(monkeypatch):
    """Optical drives present, no SCSI generic nodes — the sg-module case."""
    _fake_env(monkeypatch, ["/dev/sr0", "/dev/sr1"], [])


@pytest.fixture
def healthy_host(monkeypatch):
    """Everything visible and accessible — what the #802 reporter actually had."""
    _fake_env(monkeypatch, ["/dev/sr0", "/dev/sr1"], ["/dev/sg0", "/dev/sg1", "/dev/sg2"])


# ──────────────────────────────────────────────────────────────────────
# scsi_generic_missing
# ──────────────────────────────────────────────────────────────────────

def test_sg_missing_when_drives_exist_but_no_sg_nodes(no_sg_host):
    assert drive_registry.scsi_generic_missing() is True


def test_sg_not_missing_when_sg_nodes_exist(healthy_host):
    assert drive_registry.scsi_generic_missing() is False


def test_sg_not_missing_when_there_are_no_drives_at_all(monkeypatch):
    """No optical drives means nothing to diagnose — don't cry wolf on a NAS
    that simply has no drive attached."""
    _fake_env(monkeypatch, [], [])
    assert drive_registry.scsi_generic_missing() is False


# ──────────────────────────────────────────────────────────────────────
# diagnose_no_drives_environment — the reason matrix
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "sr, sg, host_sg, accessible, expected",
    [
        # Host genuinely has no SCSI generic support (CachyOS default).
        (["/dev/sr0"], [], [], True, "no_sg_nodes"),
        ([], [], [], True, "no_devices"),
        # Host has sg devices, container got none — the case that bit two reporters.
        (["/dev/sr0"], [], ["sg0", "sg1"], True, "sg_not_passed_through"),
        ([], [], ["sg0"], True, "sg_not_passed_through"),
        ([], ["/dev/sg0"], None, True, "no_sr_nodes"),
        (["/dev/sr0"], ["/dev/sg0"], None, False, "sg_not_accessible"),
        (["/dev/sr0"], ["/dev/sg0"], None, True, "unknown"),
    ],
)
def test_environment_reasons(monkeypatch, sr, sg, host_sg, accessible, expected):
    _fake_env(monkeypatch, sr, sg, accessible=accessible, host_sg=host_sg)
    reason, detail = drive_registry.diagnose_no_drives_environment()
    assert reason == expected
    assert detail, "every reason must carry a human-readable observation"


def test_sg_loaded_on_host_but_absent_in_container_never_says_modprobe(monkeypatch):
    """Two reporters had `sg` loaded and were still told to load it.

    A non-privileged container gets /dev/sr* (explicitly passed) and no
    /dev/sg*, while /sys/class/scsi_generic still lists the host's devices —
    measured on a real container. `docker restart` cannot fix it either, since
    --device flags are fixed at creation.
    """
    _fake_env(monkeypatch, ["/dev/sr0", "/dev/sr1"], [], host_sg=["sg0", "sg1", "sg2"])
    msg = diagnose_makemkv_no_drives(FIXTURE.read_text())
    assert msg is not None
    # The message may mention modprobe only to tell them NOT to run it.
    assert "do NOT run modprobe" in msg
    assert "sudo modprobe sg" not in msg, "must not instruct a load they already did"
    assert "docker restart` will not add them" in msg, "restarting is the obvious wrong move"
    assert "Privileged" in msg, "should name the check that settles it"


def test_reporter_environment_reports_unknown_not_a_guess(monkeypatch):
    """The #802 reporter had the sg module loaded and all three /dev/sg* nodes
    visible inside the container, running as root.

    The first cut of this diagnosis asserted "missing sg module" whenever
    MakeMKV found no drives, which would have sent them chasing a modprobe they
    had already done. When the environment looks healthy the honest answer is
    "not a device-node or permission problem" plus what to collect next.
    """
    _fake_env(monkeypatch, ["/dev/sr0", "/dev/sr1"], ["/dev/sg0", "/dev/sg1", "/dev/sg2"])
    reason, detail = drive_registry.diagnose_no_drives_environment()
    assert reason == "unknown"
    assert "not a missing device node or a permission problem" in detail


def test_environment_probe_never_raises(monkeypatch):
    def boom():
        raise OSError("procfs unavailable")

    monkeypatch.setattr(drive_registry, "_enumerate_devices", boom)
    reason, detail = drive_registry.diagnose_no_drives_environment()
    assert reason == "unknown"
    assert detail


# ──────────────────────────────────────────────────────────────────────
# diagnose_makemkv_no_drives
# ──────────────────────────────────────────────────────────────────────

def test_reported_log_is_recognised(no_sg_host):
    msg = diagnose_makemkv_no_drives(FIXTURE.read_text())
    assert msg is not None, "the log from the actual bug report must be recognised"
    assert "no usable optical drives" in msg


def test_missing_sg_names_the_module_and_says_it_is_host_side(no_sg_host):
    msg = diagnose_makemkv_no_drives(FIXTURE.read_text())
    assert "modprobe sg" in msg
    assert "/etc/modules-load.d/sg.conf" in msg
    # The single most important fact: doing this inside the container is futile.
    assert "HOST" in msg


def test_healthy_environment_never_suggests_modprobe(healthy_host):
    """The reporter's exact environment. Suggesting `modprobe sg` to someone
    whose sg nodes are already present and accessible is worse than saying
    nothing — it burns their time and our credibility."""
    msg = diagnose_makemkv_no_drives(FIXTURE.read_text())
    assert msg is not None
    assert "modprobe" not in msg
    assert "not a missing device node or a permission problem" in msg
    assert "sg_inq" in msg, "should name the next diagnostic to run"


def test_raw_stream_without_timestamps_is_recognised(no_sg_host):
    """run_makemkv keeps raw stdout in full_output; only the log file gets a
    timestamp prefix. Both must match."""
    raw = "\n".join(
        line.split("] ", 1)[1] for line in FIXTURE.read_text().splitlines() if "] " in line
    )
    assert "MSG:5042" in raw and not raw.startswith("[")
    assert diagnose_makemkv_no_drives(raw) is not None


def test_unknown_device_alone_is_enough(no_sg_host):
    """MSG:2024 without MSG:5042 still means the device could not be resolved."""
    only_2024 = 'MSG:2024,16777216,1,"Unknown device - \'/dev/sr0\'","Unknown device - \'%1\'","/dev/sr0"\n'
    assert diagnose_makemkv_no_drives(only_2024) is not None


def test_healthy_output_is_not_flagged(no_sg_host):
    """A successful scan must never be turned into a drive error, even on a
    host that happens to lack sg nodes."""
    assert diagnose_makemkv_no_drives(HEALTHY_OUTPUT) is None


def test_empty_and_none_output_are_not_flagged(no_sg_host):
    assert diagnose_makemkv_no_drives("") is None
    assert diagnose_makemkv_no_drives(None) is None


def test_diagnosis_survives_a_broken_registry(monkeypatch):
    """The probe must never be the reason a scan blows up — a failing registry
    degrades to the generic advice."""
    def boom():
        raise OSError("procfs unavailable")

    monkeypatch.setattr(drive_registry, "_enumerate_devices", boom)
    msg = diagnose_makemkv_no_drives(FIXTURE.read_text())
    assert msg is not None
    assert "no usable optical drives" in msg


# ──────────────────────────────────────────────────────────────────────
# Error type / API contract
# ──────────────────────────────────────────────────────────────────────

def test_no_drives_error_is_a_makemkv_error():
    """Existing `except MakeMKVError` handlers must keep catching it."""
    assert issubclass(MakeMKVNoDrivesError, MakeMKVError)


# ──────────────────────────────────────────────────────────────────────
# run_makemkv wiring — where the first attempt was wrong
# ──────────────────────────────────────────────────────────────────────

def _run_makemkv_with(monkeypatch, tmp_path, cmd_args, output, rc):
    """Drive run_makemkv against canned makemkvcon output."""
    import subprocess

    from core import utils

    class FakeProc:
        def __init__(self):
            self.stdout = iter(output.splitlines(keepends=True))
            self.pid = 4242

        def wait(self):
            return rc

    monkeypatch.setattr(utils, "MAKEMKVCON_PATH", "/bin/true")
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(utils, "get_rip_output_stall_seconds", lambda: 0)
    return utils.run_makemkv(cmd_args, log_path=tmp_path / "mk.log")


def test_dev_scan_raises_even_when_makemkvcon_exits_zero(monkeypatch, tmp_path, no_sg_host):
    """The bug the CachyOS VM exposed.

    `makemkvcon info dev:/dev/sr0` printed MSG:5042 and still exited 0, so the
    original wiring — which only inspected the nonzero-exit path — let it
    through. GET /discs/0/info returned **HTTP 200** with an info_log that was
    nothing but "no usable optical drives".
    """
    with pytest.raises(MakeMKVNoDrivesError) as excinfo:
        _run_makemkv_with(
            monkeypatch, tmp_path,
            "info dev:/dev/sr0 -r --minlength=0",
            FIXTURE.read_text(), rc=0,
        )
    assert "modprobe sg" in str(excinfo.value)


def test_dev_scan_raises_on_nonzero_exit_too(monkeypatch, tmp_path, no_sg_host):
    with pytest.raises(MakeMKVNoDrivesError):
        _run_makemkv_with(
            monkeypatch, tmp_path,
            "info dev:/dev/sr0 -r --minlength=0",
            FIXTURE.read_text(), rc=1,
        )


def test_disc_9999_enumeration_still_tolerates_no_drives(monkeypatch, tmp_path, no_sg_host):
    """A host with genuinely no drives must enumerate to an empty list, not
    blow up — callers of `info disc:9999` rely on that."""
    output, _ = _run_makemkv_with(
        monkeypatch, tmp_path,
        "info disc:9999 --cache=1 --minlength=0",
        FIXTURE.read_text(), rc=0,
    )
    assert "MSG:5042" in output, "output returned unchanged, no exception"


def test_a_third_dev_caller_is_covered_automatically(monkeypatch, tmp_path, no_sg_host):
    """The check lives in run_makemkv, not at the call sites.

    There were already two `info dev:` callers and the first fix wired only
    one. Any future caller is covered by construction.
    """
    with pytest.raises(MakeMKVNoDrivesError):
        _run_makemkv_with(
            monkeypatch, tmp_path,
            "info dev:/dev/sr1 -r --minlength=120",
            FIXTURE.read_text(), rc=0,
        )


def test_get_disc_info_maps_no_drives_to_503_not_409(monkeypatch):
    """409 means "a scan is already running — wait". Telling the user to wait
    for a kernel module to appear is the wrong instruction; this is a 503."""
    from fastapi import HTTPException

    from core import _drive_operations

    def fake_load(*args, **kwargs):
        raise MakeMKVNoDrivesError("MakeMKV found no usable optical drives.\n\nsudo modprobe sg")

    monkeypatch.setattr(_drive_operations, "_load_discinfo", fake_load)

    # _internal_only() allowlists 'tests', so a direct call is permitted here.
    with pytest.raises(HTTPException) as excinfo:
        _drive_operations.get_disc_info("0", "/dev/sr0")

    exc = excinfo.value
    assert exc.status_code == 503
    assert exc.detail["type"] == "no_optical_drives"
    assert "modprobe sg" in exc.detail["message"]
