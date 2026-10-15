import dataclasses
import json
import subprocess

import pytest
from ops import testing

from test_metrics import DB, KS, PKI, ctx, state

import charm

__all__ = ["ctx"]

OLD = "dev-c989722"
NEW = "dev-079a9ff"


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
                stdout = f"Getting image source signatures\nLoaded image: {f.read()}\n"
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    def systemctl(self, verb):
        return [c[2] for c in self.calls if c[:2] == ["systemctl", verb]]

    def unit(self, component):
        return self.files[f"/etc/systemd/system/ferry-{component}.service"]


@pytest.fixture
def host(monkeypatch, tmp_path):
    h = Host()
    for name in ("ETC", "PKI", "CEPH", "CONSOLE", "STATE"):
        monkeypatch.setattr(charm, name, str(tmp_path / "host" / name.lower()))
    monkeypatch.setattr(charm, "_write", h.write)
    monkeypatch.setattr(charm.subprocess, "run", h.run)
    monkeypatch.setattr(charm.FerryCharm, "_database", lambda self: DB)
    monkeypatch.setattr(charm.FerryCharm, "_keystone", lambda self: KS)
    monkeypatch.setattr(charm.FerryCharm, "_ceph", lambda self: None)
    monkeypatch.setattr(charm.FerryCharm, "_unit_pki", lambda self: PKI)
    monkeypatch.setattr(charm.FerryCharm, "_require_healthy", lambda self: None)
    return h


def resources(tmp_path, tag):
    out = set()
    for component, name in charm.IMAGE_RESOURCES:
        path = tmp_path / f"{component}-{tag or 'empty'}.tar"
        path.write_text(f"ghcr.io/osie/ferry-{component}:{tag}" if tag else "")
        out.add(testing.Resource(name=name, path=path))
    return out


def running(ctx, version):
    return ctx.run(ctx.on.config_changed(), state(config={"version": version}))


def test_podman_load_output_names_every_image_it_loaded():
    assert charm._loaded_refs("Getting image source signatures\nLoaded image: ghcr.io/osie/ferry-manager:0.15.0\n") == [
        "ghcr.io/osie/ferry-manager:0.15.0"]
    assert charm._loaded_refs("Loaded image(s): localhost/ferry-mover:dev,ghcr.io/osie/ferry-mover:dev\n") == [
        "localhost/ferry-mover:dev", "ghcr.io/osie/ferry-mover:dev"]
    assert charm._loaded_refs("Loaded image: sha256:4f2c\n") == ["sha256:4f2c"]
    assert charm._loaded_refs("") == []


def test_a_refresh_with_a_newer_image_leaves_the_running_service_alone_until_version_names_it(ctx, host, tmp_path):
    before = running(ctx, OLD)
    assert f"ferry-manager:{OLD}" in host.unit("manager")
    baseline = dict(host.files)
    host.calls.clear()

    refreshed = ctx.run(ctx.on.upgrade_charm(), dataclasses.replace(before, resources=resources(tmp_path, NEW)))

    assert refreshed.unit_status == testing.BlockedStatus(
        f"manager-image holds ghcr.io/osie/ferry-manager:{NEW} but registry and version name "
        f"ghcr.io/osie/ferry-manager:{OLD}: set them to the attached image, or attach an empty manager-image "
        f"to pull ghcr.io/osie/ferry-manager:{OLD}")
    assert host.files == baseline
    assert host.systemctl("restart") == []
    assert host.systemctl("start") == []

    host.calls.clear()
    current = ctx.run(ctx.on.config_changed(), dataclasses.replace(refreshed, config={"version": NEW}))

    assert isinstance(current.unit_status, testing.ActiveStatus)
    assert f"ferry-manager:{NEW}" in host.unit("manager")
    assert f"ferry-mover:{NEW}" in host.unit("mover")
    assert [c for c in host.calls if c == ["systemctl", "daemon-reload"]] == [["systemctl", "daemon-reload"]]
    assert host.systemctl("restart") == ["ferry-manager.service", "ferry-mover.service"]


def test_version_set_before_the_refresh_waits_for_the_image_and_switches_in_the_refresh(ctx, host, tmp_path):
    before = ctx.run(ctx.on.upgrade_charm(), state(config={"version": OLD}, resources=resources(tmp_path, OLD)))
    assert isinstance(before.unit_status, testing.ActiveStatus)
    assert f"ferry-manager:{OLD}" in host.unit("manager")
    baseline = dict(host.files)
    host.calls.clear()

    waiting = ctx.run(ctx.on.config_changed(), dataclasses.replace(before, config={"version": NEW}))

    assert isinstance(waiting.unit_status, testing.BlockedStatus)
    assert waiting.unit_status.message.startswith(f"manager-image holds ghcr.io/osie/ferry-manager:{OLD} ")
    assert host.files == baseline
    assert host.systemctl("restart") == []

    host.calls.clear()
    out = ctx.run(ctx.on.upgrade_charm(), dataclasses.replace(waiting, resources=resources(tmp_path, NEW)))

    assert isinstance(out.unit_status, testing.ActiveStatus)
    assert ["podman", "load", "-i", str(tmp_path / f"manager-{NEW}.tar")] in host.calls
    assert f"ferry-manager:{NEW}" in host.unit("manager")
    assert host.systemctl("restart") == ["ferry-manager.service", "ferry-mover.service"]


def test_an_image_held_by_the_mover_resource_alone_still_blocks(ctx, host, tmp_path):
    manager, mover = tmp_path / "manager.tar", tmp_path / "mover.tar"
    manager.write_text("")
    mover.write_text(f"ghcr.io/osie/ferry-mover:{NEW}")
    out = ctx.run(ctx.on.upgrade_charm(), dataclasses.replace(running(ctx, OLD), resources={
        testing.Resource(name="manager-image", path=manager), testing.Resource(name="mover-image", path=mover)}))
    assert isinstance(out.unit_status, testing.BlockedStatus)
    assert out.unit_status.message.startswith(f"mover-image holds ghcr.io/osie/ferry-mover:{NEW} ")


def test_an_empty_resource_hands_the_image_back_to_version(ctx, host, tmp_path):
    blocked = ctx.run(ctx.on.upgrade_charm(), dataclasses.replace(running(ctx, OLD), resources=resources(tmp_path, NEW)))
    assert isinstance(blocked.unit_status, testing.BlockedStatus)

    out = ctx.run(ctx.on.upgrade_charm(), dataclasses.replace(blocked, resources=resources(tmp_path, "")))

    assert isinstance(out.unit_status, testing.ActiveStatus)
    assert f"ferry-manager:{OLD}" in host.unit("manager")
    assert not [c for c in host.calls if c[:2] == ["podman", "load"] and c[3].endswith("empty.tar")]


@pytest.mark.parametrize("config, drain", [({}, 90), ({"mover-drain-timeout": 300}, 300)])
def test_the_mover_unit_waits_out_the_drain_before_it_kills_the_container(ctx, host, config, drain):
    out = ctx.run(ctx.on.config_changed(), state(config={"version": OLD, **config}))

    assert isinstance(out.unit_status, testing.ActiveStatus)
    assert json.loads(host.files[f"{charm.ETC}/mover.yaml"])["drain_timeout"] == f"{drain}s"
    lines = host.unit("mover").splitlines()
    assert f"ExecStop=/usr/bin/podman stop -t {drain + 60} ferry-mover" in lines
    assert f"TimeoutStopSec={drain + 90}" in lines
    assert "ExecStop=/usr/bin/podman stop -t 30 ferry-manager" in host.unit("manager").splitlines()
