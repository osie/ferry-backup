#!/usr/bin/env python3
import json
import logging
import os
import pwd
import re
import socket
import subprocess
import time
import urllib.request

import ops

ETC = "/etc/ferry-compute"
PKI = "/etc/ferry-compute/pki"
STATE = "/var/lib/ferry/compute"
SNAP = "/var/snap/openstack-hypervisor/common"
APPARMOR = "/etc/apparmor.d"
APPARMOR_SNIPPET = f"{APPARMOR}/abstractions/libvirt-qemu.d/ferry-compute"
ADMIN_PORT = 9780
LOADED = re.compile(r"^Loaded image(?:\(s\))?:\s*(.+)$")

log = logging.getLogger(__name__)


def _write(path, content, mode=0o644):
    try:
        with open(path) as f:
            if f.read() == content:
                os.chmod(path, mode)
                return False
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(content)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    return True


def _reload_guest_profiles():
    profiles = f"{APPARMOR}/libvirt"
    if not os.path.isdir(profiles):
        return
    for name in sorted(os.listdir(profiles)):
        if not name.startswith("libvirt-") or name.endswith(".files"):
            continue
        r = subprocess.run(["apparmor_parser", "-r", f"{profiles}/{name}"], capture_output=True, text=True)
        if r.returncode != 0:
            log.warning("reloading %s failed: %s", name, r.stderr.strip())


def _loaded_refs(output):
    refs = []
    for line in (output or "").splitlines():
        m = LOADED.match(line.strip())
        if m:
            refs.extend(r.strip() for r in m.group(1).split(",") if r.strip())
    return refs


class Waiting(Exception):
    pass


class Blocked(Exception):
    pass


class Layout:
    def __init__(self, config):
        snap = os.path.isdir(SNAP)
        self.snap = snap
        if snap:
            self.socket = f"{SNAP}/run/libvirt/libvirt-sock"
            self.scratch = f"{SNAP}/lib/ferry/scratch"
            self.sockets = f"{SNAP}/lib/ferry/sockets"
            self.qemu = "root"
            self.mounts = [f"{SNAP}:{SNAP}"]
        else:
            self.socket = "/run/libvirt/libvirt-sock"
            self.scratch = "/var/lib/libvirt/ferry/scratch"
            self.sockets = "/var/lib/libvirt/ferry/sockets"
            self.qemu = "libvirt-qemu"
            self.mounts = ["/run/libvirt:/run/libvirt", "/var/lib/libvirt:/var/lib/libvirt", "/var/lib/nova:/var/lib/nova"]
        if config["libvirt-socket"]:
            self.socket = config["libvirt-socket"]
            run = os.path.dirname(self.socket)
            if not any(m.startswith(run + ":") or run.startswith(m.split(":")[0] + "/") for m in self.mounts):
                self.mounts.append(f"{run}:{run}")
        if config["scratch-dir"]:
            self.scratch = config["scratch-dir"]
            self.sockets = os.path.join(os.path.dirname(self.scratch), "sockets")
        if config["qemu-user"]:
            self.qemu = config["qemu-user"]


class FerryComputeCharm(ops.CharmBase):
    _stored = ops.StoredState()

    def __init__(self, framework):
        super().__init__(framework)
        self._stored.set_default(images=[])
        framework.observe(self.on.install, self._on_install)
        framework.observe(self.on.upgrade_charm, self._on_install)
        for event in (
            self.on.config_changed,
            self.on.leader_elected,
            self.on.update_status,
            self.on.juju_info_relation_joined,
            self.on.manager_relation_joined,
            self.on.manager_relation_changed,
            self.on.manager_relation_broken,
            self.on.metrics_endpoint_relation_joined,
            self.on.metrics_endpoint_relation_changed,
        ):
            framework.observe(event, self._reconcile)
        framework.observe(self.on.stop, self._on_stop)

    def _on_install(self, event):
        self.unit.status = ops.MaintenanceStatus("installing podman")
        subprocess.run(["apt-get", "update", "-q"], check=True, capture_output=True)
        subprocess.run(
            ["apt-get", "install", "-y", "-q", "podman"],
            check=True,
            capture_output=True,
            env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
        )
        self._load_resource()
        self._reconcile(event)

    def _load_resource(self):
        loaded = []
        try:
            path = self.model.resources.fetch("compute-image")
            if path and os.path.getsize(path) > 0:
                self.unit.status = ops.MaintenanceStatus("loading the agent image")
                out = subprocess.run(["podman", "load", "-i", str(path)], check=True, capture_output=True, text=True)
                loaded = _loaded_refs(f"{out.stdout}\n{out.stderr}")
        except (ops.ModelError, NameError):
            pass
        self._stored.images = loaded

    def _require_attached_image(self, want):
        held = list(self._stored.images)
        if held and want not in held:
            raise Blocked(
                f"compute-image holds {', '.join(held)} but the ferry application runs {want}: "
                f"set its version to the attached image, or attach an empty compute-image to pull {want}")

    def _on_stop(self, event):
        subprocess.run(["systemctl", "disable", "--now", "ferry-compute.service"], capture_output=True)
        if os.path.exists(APPARMOR_SNIPPET):
            os.remove(APPARMOR_SNIPPET)
            _reload_guest_profiles()

    def _address(self):
        binding = self.model.get_binding("juju-info")
        if binding and binding.network.bind_address:
            return str(binding.network.bind_address)
        return socket.gethostbyname(socket.getfqdn())

    def _metrics_address(self):
        binding = self.model.get_binding("metrics-endpoint")
        if binding and binding.network.bind_address:
            return str(binding.network.bind_address)
        return self._address()

    def _admin_host(self, m):
        return self._metrics_address() if m["expose-metrics"] else "127.0.0.1"

    def _publish_metrics(self, m):
        jobs = [{
            "job_name": "ferry_compute",
            "metrics_path": "/metrics",
            "static_configs": [{"targets": [f"*:{ADMIN_PORT}"]}],
            "relabel_configs": [{"target_label": "job", "replacement": "ferry_compute"}],
        }] if m["expose-metrics"] else []
        for rel in self.model.relations["metrics-endpoint"]:
            rel.data[self.unit].update({
                "prometheus_scrape_unit_address": self._metrics_address(),
                "prometheus_scrape_unit_name": self.unit.name,
            })
            if self.unit.is_leader():
                rel.data[self.app].update({
                    "scrape_metadata": json.dumps({
                        "model": self.model.name, "model_uuid": self.model.uuid, "application": self.app.name,
                        "unit": self.unit.name, "charm_name": self.meta.name,
                    }),
                    "scrape_jobs": json.dumps(jobs),
                    "alert_rules": json.dumps({}),
                })

    def _manager(self):
        rel = self.model.get_relation("manager")
        if not rel:
            raise Waiting("relate manager to the ferry application")
        rel.data[self.unit].update({"hostname": socket.getfqdn(), "address": self._address()})
        d = rel.data[rel.app] if rel.app else {}
        need = ("ca", "cert", "key", "server-name", "client-cns", "port", "image")
        if not all(d.get(k) for k in need):
            raise Waiting("waiting for the ferry leader to publish the agent's identity")
        return {k: d[k] for k in need} | {
            "local-disk-backup": d.get("local-disk-backup") == "true",
            "expose-metrics": d.get("expose-metrics") == "true",
        }

    def _reconcile(self, event):
        try:
            m = self._manager()
            self._publish_metrics(m)
            layout = Layout(self.config)
            if not os.path.exists(layout.socket):
                raise Waiting(f"no libvirt socket at {layout.socket}")
            self._require_attached_image(m["image"])
            changed = self._render(m, layout)
            self._run(changed)
            self._require_healthy(self._admin_host(m))
            idle = self.model.relations["metrics-endpoint"] and not m["expose-metrics"]
            hint = "; metrics-endpoint scrapes nothing until the ferry application sets expose-metrics=true" if idle else ""
            self.unit.status = ops.ActiveStatus(f"serving on {self._address()}:{m['port']}{hint}")
        except Waiting as w:
            self.unit.status = ops.WaitingStatus(str(w))
        except Blocked as b:
            self.unit.status = ops.BlockedStatus(str(b))

    def _render(self, m, layout):
        changed = False
        for name, key, mode in (("ca.crt", "ca", 0o644), ("tls.crt", "cert", 0o644), ("tls.key", "key", 0o600)):
            changed |= _write(f"{PKI}/{name}", m[key], mode)
        os.chmod(PKI, 0o700)
        os.makedirs(STATE, mode=0o700, exist_ok=True)
        qemu = pwd.getpwnam(layout.qemu)
        for d in (layout.scratch, layout.sockets):
            os.makedirs(os.path.dirname(d), mode=0o755, exist_ok=True)
            os.makedirs(d, exist_ok=True)
            os.chown(d, qemu.pw_uid, qemu.pw_gid)
            os.chmod(d, 0o770)
        if not layout.snap and os.path.exists(f"{APPARMOR}/abstractions/libvirt-qemu"):
            rules = "".join(f"  {d}/ r,\n  {d}/** rwk,\n" for d in (layout.sockets, layout.scratch))
            if _write(APPARMOR_SNIPPET, rules):
                _reload_guest_profiles()
        backup = {"enabled": m["local-disk-backup"]}
        if m["local-disk-backup"]:
            backup.update({
                "scratch_dir": layout.scratch,
                "socket_dir": layout.sockets,
                "state_file": f"{STATE}/backups.json",
                "max_duration": "24h",
                "default_ttl": "1h",
                "max_concurrent": 2,
            })
        config = {
            "log": {"level": "info", "format": "json"},
            "api": {"bind": f"0.0.0.0:{m['port']}", "tls": {
                "enabled": True, "cert_file": f"{PKI}/tls.crt", "key_file": f"{PKI}/tls.key",
                "client_ca_file": f"{PKI}/ca.crt", "require_client_cert": True,
                "allowed_client_cns": json.loads(m["client-cns"]), "min_version": "1.3"}},
            "admin": {"enabled": True, "bind": f"{self._admin_host(m)}:{ADMIN_PORT}", "metrics": True, "pprof": False},
            "libvirt": {"socket": layout.socket, "uri": "qemu:///system"},
            "freeze": {"max_duration": "30s", "state_file": f"{STATE}/freezes.json"},
            "backup": backup,
        }
        changed |= _write(f"{ETC}/compute.yaml", json.dumps(config, indent=2, sort_keys=True) + "\n")
        env = {"FERRY_NODE_ID": socket.getfqdn(), "FERRY_API_BIND": f"{self._address()}:{m['port']}"}
        changed |= _write(f"{ETC}/compute.env", "".join(f"{k}={v}\n" for k, v in sorted(env.items())), 0o600)
        args = [
            "/usr/bin/podman", "run", "--rm", "--name=ferry-compute", "--network=host", "--pull=missing",
            "--security-opt=apparmor=unconfined", "--log-driver=none",
            f"--env-file={ETC}/compute.env",
            f"-v={ETC}/compute.yaml:/etc/ferry/compute.yaml:ro",
            f"-v={PKI}:{PKI}:ro",
            f"-v={STATE}:{STATE}",
        ] + [f"-v={mount}" for mount in layout.mounts] + [m["image"], "--config=/etc/ferry/compute.yaml"]
        unit = "\n".join([
            "[Unit]",
            "Description=Ferry compute agent",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Restart=always",
            "RestartSec=5",
            "ExecStartPre=-/usr/bin/podman rm -f ferry-compute",
            "ExecStart=" + " ".join(args),
            "ExecStop=/usr/bin/podman stop -t 30 ferry-compute",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ])
        if _write("/etc/systemd/system/ferry-compute.service", unit):
            subprocess.run(["systemctl", "daemon-reload"], check=True)
            changed = True
        return changed

    def _require_healthy(self, host):
        deadline = time.monotonic() + 60
        while True:
            try:
                with urllib.request.urlopen(f"http://{host}:{ADMIN_PORT}/healthz", timeout=5) as r:
                    if r.status == 200:
                        return
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise Waiting("ferry-compute is not answering yet; see journalctl -u ferry-compute")
            time.sleep(3)

    def _run(self, changed):
        subprocess.run(["systemctl", "enable", "ferry-compute.service"], check=True, capture_output=True)
        subprocess.run(["systemctl", "restart" if changed else "start", "ferry-compute.service"], check=True)


if __name__ == "__main__":
    ops.main(FerryComputeCharm)
