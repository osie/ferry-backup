import dataclasses
import getpass
import json
import subprocess

import pytest
from ops import testing

from test_metrics import IDENTITY, MODEL, METRICS_NETWORK

import charm

OLD = "ghcr.io/osie/ferry-compute:dev-c989722"
NEW = "ghcr.io/osie/ferry-compute:dev-079a9ff"


class Host:
    def __init__(self):
        self.files = {}
        self.calls = []

    def write(self, path, content, mode=0o644):
        if self.files.get(path) == content:
            return False
        self.files[path] = content
        return True

    def run(self, args, **kwargs):
        self.calls.append(list(args))
        stdout = ""
        if args[:2] == ["podman", "load"]:
            with open(args[3]) as f:
                stdout = f"Loaded image: {f.read()}\n"
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    def restarts(self):
        return [c[2] for c in self.calls if c[:2] == ["systemctl", "restart"]]

    def unit(self):
        return self.files["/etc/systemd/system/ferry-compute.service"]


@pytest.fixture
def host(monkeypatch, tmp_path):
    h = Host()
    for name in ("ETC", "PKI", "STATE"):
        monkeypatch.setattr(charm, name, str(tmp_path / "host" / name.lower()))
    (tmp_path / "host" / "pki").mkdir(parents=True)
    monkeypatch.setattr(charm, "_write", h.write)
    monkeypatch.setattr(charm.subprocess, "run", h.run)
    monkeypatch.setattr(charm.FerryComputeCharm, "_require_healthy", lambda self, host: None)
    return h


@pytest.fixture
def ctx():
    return testing.Context(charm.FerryComputeCharm)


@pytest.fixture
def config(tmp_path):
    sock = tmp_path / "libvirt.sock"
    sock.write_text("")
    return {"scratch-dir": str(tmp_path / "libvirt" / "scratch"), "qemu-user": getpass.getuser(), "libvirt-socket": str(sock)}


def manager(image):
    return testing.Relation("manager", remote_app_name="ferry", remote_app_data=dict(IDENTITY, image=image))


def image(tmp_path, ref):
    path = tmp_path / f"compute-{ref.rsplit(':', 1)[1]}.tar"
    path.write_text(ref)
    return {testing.Resource(name="compute-image", path=path)}


def test_a_refreshed_agent_image_waits_for_the_ferry_application_to_name_it(ctx, config, host, tmp_path):
    rel = manager(OLD)
    before = ctx.run(ctx.on.config_changed(), testing.State(
        model=MODEL, networks={METRICS_NETWORK}, config=config, relations={rel}))
    assert OLD in host.unit()
    baseline = dict(host.files)
    host.calls.clear()

    refreshed = ctx.run(ctx.on.upgrade_charm(), dataclasses.replace(before, resources=image(tmp_path, NEW)))

    assert refreshed.unit_status == testing.BlockedStatus(
        f"compute-image holds {NEW} but the ferry application runs {OLD}: set its version to the attached image, "
        f"or attach an empty compute-image to pull {OLD}")
    assert host.files == baseline
    assert host.restarts() == []

    host.calls.clear()
    named = dataclasses.replace(rel, remote_app_data=dict(IDENTITY, image=NEW))
    out = ctx.run(ctx.on.relation_changed(named, remote_unit=0), dataclasses.replace(refreshed, relations={named}))

    assert isinstance(out.unit_status, testing.ActiveStatus)
    assert NEW in host.unit()
    assert host.restarts() == ["ferry-compute.service"]
    assert json.loads(host.files[f"{charm.ETC}/compute.yaml"])["libvirt"]["socket"] == config["libvirt-socket"]
