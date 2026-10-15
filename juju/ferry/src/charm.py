#!/usr/bin/env python3
import base64
import binascii
import json
import os
import re
import secrets
import socket
import subprocess
import time
import urllib.request
import uuid

import ops
import yaml

import pki

ETC = "/etc/ferry"
PKI = "/etc/ferry/pki"
CEPH = "/etc/ferry/ceph"
CONSOLE = "/etc/ferry/console"
CONSOLE_FILES = (
    ("console-logo", "logo", "logo"),
    ("console-logo-dark", "logo-dark", "logo_dark"),
    ("console-favicon", "favicon", "favicon"),
    ("console-stylesheet", "theme.css", "stylesheet"),
)
CONSOLE_OPTIONS = (
    ("console-product-name", "product_name"),
    ("console-primary-color", "primary_color"),
    ("console-primary-color-dark", "primary_color_dark"),
)
STATE = "/var/lib/ferry"
COMPUTE_NAME = "ferry-compute"
MANAGER_CLIENT_CN = "ferry-manager-client"
DB_NAME = "ferry"
DB_USER = "ferry"
SECRET_FIELDS = ["username", "password", "tls", "tls-ca", "uris", "read-only-uris"]
METRICS_JOBS = (("ferry_manager", 9775), ("ferry_mover", 9782))
ALERT_RULES = "src/prometheus_alert_rules/ferry.rules"
IMAGE_RESOURCES = (("manager", "manager-image"), ("mover", "mover-image"))
LOADED = re.compile(r"^Loaded image(?:\(s\))?:\s*(.+)$")


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


def _put(path, content):
    try:
        with open(path, "rb") as f:
            if f.read() == content:
                return False
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(content)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return True


def _csv(value):
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _yaml(value):
    return json.dumps(value, indent=2, sort_keys=True)


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


class FerryCharm(ops.CharmBase):
    _stored = ops.StoredState()

    def __init__(self, framework):
        super().__init__(framework)
        self._stored.set_default(images={})
        framework.observe(self.on.install, self._on_install)
        framework.observe(self.on.upgrade_charm, self._on_install)
        for event in (
            self.on.config_changed,
            self.on.leader_elected,
            self.on.update_status,
            self.on.secret_changed,
            self.on.peers_relation_joined,
            self.on.peers_relation_changed,
            self.on.peers_relation_departed,
            self.on.shared_db_relation_joined,
            self.on.shared_db_relation_changed,
            self.on.shared_db_relation_broken,
            self.on.database_relation_joined,
            self.on.database_relation_changed,
            self.on.database_relation_broken,
            self.on.identity_credentials_relation_joined,
            self.on.identity_credentials_relation_changed,
            self.on.identity_credentials_relation_broken,
            self.on.ceph_relation_joined,
            self.on.ceph_relation_changed,
            self.on.ceph_relation_broken,
            self.on.ceph_mover_relation_joined,
            self.on.ceph_mover_relation_changed,
            self.on.ceph_mover_relation_broken,
            self.on.ceph_restore_relation_joined,
            self.on.ceph_restore_relation_changed,
            self.on.ceph_restore_relation_broken,
            self.on.compute_relation_joined,
            self.on.compute_relation_changed,
            self.on.metrics_endpoint_relation_joined,
            self.on.metrics_endpoint_relation_changed,
        ):
            framework.observe(event, self._reconcile)

    def _on_install(self, event):
        self.unit.status = ops.MaintenanceStatus("installing podman")
        subprocess.run(["apt-get", "update", "-q"], check=True, capture_output=True)
        subprocess.run(
            ["apt-get", "install", "-y", "-q", "podman", "openssl"],
            check=True,
            capture_output=True,
            env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
        )
        for d in (ETC, PKI, CEPH, CONSOLE, STATE, f"{STATE}/mover"):
            os.makedirs(d, exist_ok=True)
        os.chmod(PKI, 0o700)
        self._load_resources()
        self._reconcile(event)

    def _load_resources(self):
        loaded = {}
        for component, name in IMAGE_RESOURCES:
            try:
                path = self.model.resources.fetch(name)
            except (ops.ModelError, NameError):
                continue
            if path and os.path.getsize(path) > 0:
                self.unit.status = ops.MaintenanceStatus(f"loading {name}")
                out = subprocess.run(["podman", "load", "-i", str(path)], check=True, capture_output=True, text=True)
                loaded[component] = _loaded_refs(f"{out.stdout}\n{out.stderr}")
        self._stored.images = loaded

    def _require_attached_images(self):
        for component, name in IMAGE_RESOURCES:
            held = list(self._stored.images.get(component, []))
            want = self._image(component)
            if held and want not in held:
                raise Blocked(
                    f"{name} holds {', '.join(held)} but registry and version name {want}: "
                    f"set them to the attached image, or attach an empty {name} to pull {want}")

    @property
    def _peers(self):
        return self.model.get_relation("peers")

    def _address(self):
        binding = self.model.get_binding("peers")
        if binding and binding.network.ingress_address:
            return str(binding.network.ingress_address)
        return socket.gethostbyname(socket.gethostname())

    def _metrics_address(self):
        binding = self.model.get_binding("metrics-endpoint")
        if binding and binding.network.bind_address:
            return str(binding.network.bind_address)
        return self._address()

    def _admin_host(self):
        return self._metrics_address() if self.config["expose-metrics"] else "127.0.0.1"

    def _console(self):
        console, wrote = {}, False
        for option, filename, key in CONSOLE_FILES:
            target = f"{CONSOLE}/{filename}"
            value = self.config[option].strip()
            if value:
                try:
                    content = base64.b64decode(value, validate=True)
                except (binascii.Error, ValueError):
                    raise Blocked(f"{option} is not base64: set it to the output of base64 -w0 <file>")
                wrote |= _put(target, content)
                console[key] = target
            elif os.path.exists(target):
                os.remove(target)
                wrote = True
        for option, key in CONSOLE_OPTIONS:
            if self.config[option]:
                console[key] = self.config[option]
        if self.config["console-logo-only"] and "logo" in console:
            console["logo_only"] = True
            if self.config["console-logo-align"]:
                console["logo_align"] = self.config["console-logo-align"]
            for option, key in (("console-logo-height", "logo_height"), ("console-logo-width", "logo_width")):
                if self.config[option] > 0:
                    console[key] = self.config[option]
        return console, wrote

    def _image(self, component):
        return f"{self.config['registry']}/ferry-{component}:{self.config['version']}"

    def _cloud_name(self):
        return self.config["cloud-name"] or self.model.name

    def _reconcile(self, event):
        try:
            self._publish_requests()
            self._publish_metrics()
            if self.unit.is_leader():
                self._leader_state()
            db = self._database()
            ks = self._keystone()
            ceph = self._ceph()
            unit_pki = self._unit_pki()
            self._require_attached_images()
            changed = self._render(db, ks, ceph, unit_pki)
            self._run_services(changed)
            if self.unit.is_leader():
                self._publish_compute()
            self._require_healthy()
            idle = self.model.relations["metrics-endpoint"] and not self.config["expose-metrics"]
            hint = "; metrics-endpoint scrapes nothing until expose-metrics=true" if idle else ""
            self.unit.status = ops.ActiveStatus(f"serving {self._public_url()}{hint}")
        except Waiting as w:
            self.unit.status = ops.WaitingStatus(str(w))
        except Blocked as b:
            self.unit.status = ops.BlockedStatus(str(b))

    def _publish_requests(self):
        address = self._address()
        if self._peers:
            self._peers.data[self.unit]["address"] = address
            self._peers.data[self.unit]["hostname"] = socket.getfqdn()
        for rel in self.model.relations["shared-db"]:
            rel.data[self.unit].update({"database": DB_NAME, "username": DB_USER, "hostname": address})
        for rel in self.model.relations["database"]:
            if self.unit.is_leader():
                rel.data[self.app].update({
                    "database": DB_NAME,
                    "external-node-connectivity": "true",
                    "requested-secrets": json.dumps(SECRET_FIELDS),
                })
        for rel in self.model.relations["identity-credentials"]:
            rel.data[self.unit]["username"] = self.app.name
            if self.unit.is_leader():
                rel.data[self.app]["username"] = self.app.name
        for endpoint, (client, osd) in self._ceph_identities().items():
            for rel in self.model.relations[endpoint]:
                if endpoint != "ceph":
                    rel.data[self.unit]["application-name"] = client
                rel.data[self.unit]["broker_req"] = self._broker_request(rel, client, osd)

    def _ceph_identities(self):
        written = _csv(self.config["volume-pools"]) + _csv(self.config["vm-pools"])
        images = _csv(self.config["image-pools"])
        rw = [f"profile rbd pool={p}" for p in written] + [f"profile rbd-read-only pool={p}" for p in images]
        ro = [f"profile rbd-read-only pool={p}" for p in written + images]
        return {
            "ceph": (self.app.name, ", ".join(rw) or "profile rbd"),
            "ceph-mover": (f"{self.app.name}-mover", ", ".join(ro) or "profile rbd-read-only"),
            "ceph-restore": (f"{self.app.name}-restore", ", ".join(rw) or "profile rbd"),
        }

    def _broker_request(self, rel, client, osd):
        current = rel.data[self.unit].get("broker_req")
        ops_list = [{
            "op": "set-key-permissions",
            "client": client,
            "permissions": ["mon", "profile rbd", "osd", osd],
        }]
        sources = [rel.data[u] for u in rel.units] + ([rel.data[rel.app]] if rel.app else [])
        keyed = any(d.get("key") for d in sources)
        resend = keyed and rel.data[self.unit].get("requested-with-key") != "true"
        if keyed:
            rel.data[self.unit]["requested-with-key"] = "true"
        if current and not resend:
            try:
                prev = json.loads(current)
                if prev.get("ops") == ops_list:
                    return current
            except ValueError:
                pass
        return json.dumps({"api-version": 1, "request-id": str(uuid.uuid4()), "ops": ops_list}, sort_keys=True)

    def _publish_metrics(self):
        jobs, rules = [], {}
        if self.config["expose-metrics"]:
            jobs = [{
                "job_name": name,
                "metrics_path": "/metrics",
                "static_configs": [{"targets": [f"*:{port}"]}],
                "relabel_configs": [{"target_label": "job", "replacement": name}],
                "metric_relabel_configs": [{"action": "labeldrop", "regex": "juju_unit"}],
            } for name, port in METRICS_JOBS]
            with open(self.charm_dir / ALERT_RULES) as f:
                rules = yaml.safe_load(f)
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
                    "alert_rules": json.dumps(rules),
                })

    def _leader_state(self):
        if not self._peers:
            raise Waiting("waiting for the peer relation")
        data = self._peers.data[self.app]
        if not data.get("ca"):
            ca, ca_key = pki.make_ca(f"ferry-{self._cloud_name()}-ca")
            data.update({"ca": ca, "ca-key": ca_key})
        ca, ca_key = data["ca"], data["ca-key"]
        if not data.get("appcred-secret"):
            data["appcred-secret"] = secrets.token_urlsafe(36)
        if pki.expiring(data.get("manager-client")):
            cert, key = pki.issue(ca, ca_key, MANAGER_CLIENT_CN, "client")
            data.update({"manager-client": cert, "manager-client-key": key})
        if pki.expiring(data.get("compute")):
            cert, key = pki.issue(ca, ca_key, COMPUTE_NAME, "server", [COMPUTE_NAME])
            data.update({"compute": cert, "compute-key": key})
        units = [self.unit] + list(self._peers.units)
        names = {"127.0.0.1", "localhost"}
        for u in units:
            for field in ("address", "hostname"):
                if self._peers.data[u].get(field):
                    names.add(self._peers.data[u][field])
        sans = ",".join(sorted(names))
        if data.get("mover-api-sans") != sans or pki.expiring(data.get("mover-api")):
            cert, key = pki.issue(ca, ca_key, "ferry-mover-api", "server", sorted(names))
            data.update({"mover-api": cert, "mover-api-key": key, "mover-api-sans": sans})
        movers = json.loads(data.get("movers") or "{}")
        grown = False
        for u in units:
            mid = self._mover_id(u)
            if mid not in movers or pki.expiring(movers[mid]["cert"]):
                cert, key = pki.issue(ca, ca_key, mid, "client")
                movers[mid] = {"cert": cert, "key": key}
                grown = True
        if grown:
            data["movers"] = json.dumps(movers, sort_keys=True)

    def _mover_id(self, unit):
        return "ferry-mover-" + unit.name.split("/")[1]

    def _mover_drain(self):
        drain = int(self.config.get("mover-drain-timeout") or 0)
        return drain if drain > 0 else 90

    def _unit_pki(self):
        if not self._peers:
            raise Waiting("waiting for the peer relation")
        data = self._peers.data[self.app]
        movers = json.loads(data.get("movers") or "{}")
        mine = movers.get(self._mover_id(self.unit))
        if not data.get("ca") or not mine or not data.get("mover-api") or not data.get("appcred-secret"):
            raise Waiting("waiting for the leader to issue this unit's certificates")
        if self._address() not in (data.get("mover-api-sans") or "").split(","):
            raise Waiting("waiting for the leader to name this unit in the manager certificate")
        return {
            "ca": data["ca"],
            "mover-api": data["mover-api"], "mover-api-key": data["mover-api-key"],
            "manager-client": data["manager-client"], "manager-client-key": data["manager-client-key"],
            "mover": mine["cert"], "mover-key": mine["key"],
            "appcred-secret": data["appcred-secret"],
        }

    def _secret_content(self, uri):
        try:
            return self.model.get_secret(id=uri).get_content(refresh=True)
        except (ops.SecretNotFoundError, ops.ModelError):
            return {}

    def _database(self):
        for rel in self.model.relations["shared-db"]:
            for unit in rel.units:
                d = rel.data[unit]
                if d.get("password") and d.get("db_host"):
                    allowed = d.get("allowed_units", "")
                    if allowed and self.unit.name not in allowed.split():
                        continue
                    port = d.get("db_port") or "3306"
                    return {"user": DB_USER, "password": d["password"], "endpoint": f"{d['db_host']}:{port}"}
        for rel in self.model.relations["database"]:
            if not rel.app:
                continue
            d = rel.data[rel.app]
            endpoint = (d.get("endpoints") or "").split(",")[0]
            user, password = d.get("username"), d.get("password")
            if d.get("secret-user"):
                content = self._secret_content(d["secret-user"])
                user, password = content.get("username"), content.get("password")
            if endpoint and user and password:
                return {"user": user, "password": password, "endpoint": endpoint}
        if not self.model.relations["shared-db"] and not self.model.relations["database"]:
            raise Waiting("relate shared-db (mysql-router) or database (mysql_client)")
        raise Waiting("waiting for the database")

    def _keystone(self):
        for rel in self.model.relations["identity-credentials"]:
            if rel.app and rel.data[rel.app].get("credentials"):
                d = rel.data[rel.app]
                content = self._secret_content(d["credentials"])
                if content.get("username") and content.get("password"):
                    return {
                        "auth_url": d.get("internal-endpoint") or d.get("public-endpoint"),
                        "username": content["username"],
                        "password": content["password"],
                        "project": d.get("project-name", "services"),
                        "user_domain": d.get("user-domain-name", "service_domain"),
                        "project_domain": d.get("project-domain-name", "service_domain"),
                        "region": d.get("region", "RegionOne"),
                    }
            for unit in rel.units:
                d = rel.data[unit]
                if d.get("credentials_password"):
                    proto = d.get("credentials_protocol", "http")
                    host, port = d.get("credentials_host"), d.get("credentials_port", "5000")
                    return {
                        "auth_url": f"{proto}://{host}:{port}/v3",
                        "username": d["credentials_username"],
                        "password": d["credentials_password"],
                        "project": d.get("credentials_project", "services"),
                        "user_domain": d.get("credentials_user_domain_name") or d.get("domain") or "service_domain",
                        "project_domain": d.get("credentials_project_domain_name") or d.get("domain") or "service_domain",
                        "region": d.get("region", "RegionOne"),
                    }
        if not self.model.relations["identity-credentials"]:
            raise Waiting("relate identity-credentials (keystone)")
        raise Waiting("waiting for keystone credentials")

    def _ceph(self):
        if not self.model.relations["ceph"]:
            return None
        keys, mons = {}, []
        for endpoint, (client, _) in self._ceph_identities().items():
            rels = self.model.relations[endpoint]
            if not rels:
                continue
            key = None
            for rel in rels:
                sources = [rel.data[u] for u in rel.units] + ([rel.data[rel.app]] if rel.app else [])
                for d in sources:
                    key = key or d.get("key")
                    if d.get("ceph-public-address"):
                        mons.append(d["ceph-public-address"])
                    mons.extend(_csv(d.get("mon_hosts", "").replace(" ", ",")))
            if not key:
                raise Waiting(f"waiting for ceph to issue client.{client}")
            keys[client] = key
        if not mons:
            raise Waiting("waiting for ceph to name its monitors")
        return {"keys": keys, "mons": sorted(set(mons))}

    def _public_url(self):
        return self.config["public-url"] or f"http://{self._address()}:9776"

    def _render(self, db, ks, ceph, p):
        changed = set()
        files = {
            f"{PKI}/ca.crt": (p["ca"], 0o644),
            f"{PKI}/mover-api.crt": (p["mover-api"], 0o644),
            f"{PKI}/mover-api.key": (p["mover-api-key"], 0o600),
            f"{PKI}/manager-client.crt": (p["manager-client"], 0o644),
            f"{PKI}/manager-client.key": (p["manager-client-key"], 0o600),
            f"{PKI}/mover.crt": (p["mover"], 0o644),
            f"{PKI}/mover.key": (p["mover-key"], 0o600),
        }
        for path, (content, mode) in files.items():
            if _write(path, content, mode):
                changed.update({"manager", "mover"})
        user = self.app.name
        mover_user = restore_user = user
        if ceph:
            wrote = _write(f"{CEPH}/ceph.conf", "[global]\nmon_host = " + ",".join(ceph["mons"]) + "\n")
            for client, key in ceph["keys"].items():
                wrote |= _write(f"{CEPH}/ceph.client.{client}.keyring", f"[client.{client}]\n  key = {key}\n", 0o640)
            if wrote:
                changed.update({"manager", "mover"})
            mover_user = f"{user}-mover" if f"{user}-mover" in ceph["keys"] else user
            restore_user = f"{user}-restore" if f"{user}-restore" in ceph["keys"] else user
        site = self.config["site-name"] or self._cloud_name()
        manager = {
            "mode": "local",
            "cloud_name": self._cloud_name(),
            "site_name": site,
            "log": {"level": "info", "format": "json"},
            "api": {"bind": "0.0.0.0:9776", "tls": {"enabled": False}},
            "mover_api": {"bind": "0.0.0.0:9777", "tls": {
                "enabled": True, "cert_file": f"{PKI}/mover-api.crt", "key_file": f"{PKI}/mover-api.key",
                "client_ca_file": f"{PKI}/ca.crt", "require_client_cert": True,
                "allowed_client_cns": ["ferry-mover-*"], "min_version": "1.3"}},
            "admin": {"enabled": True, "bind": f"{self._admin_host()}:9775", "metrics": True, "pprof": False},
            "db": {"migrate": True, "assert_version": True},
            "keystone": {
                "auth_url": ks["auth_url"],
                "application_credential_name": f"{self.app.name}-manager",
                "username": ks["username"],
                "user_domain_name": ks["user_domain"],
                "project_name": ks["project"],
                "project_domain_name": ks["project_domain"],
                "region": self.config["region"] or ks["region"],
                "interface": self.config["keystone-interface"],
                "register_in_catalog": True,
                "service_name": self.app.name,
                "service_type": self.config["service-type"],
                "public_url": self._public_url(),
                "internal_url": self._public_url(),
                "admin_url": self._public_url(),
            },
            "kms": {"driver": self.config["kms-driver"]},
            "compute": {
                "enabled": True, "scheme": "https", "port": self.config["compute-port"], "timeout": "30s",
                "hosts_from_nova": True,
                "local_disk": {"enabled": self.config["local-disk-backup"]},
                "tls": {"cert_file": f"{PKI}/manager-client.crt", "key_file": f"{PKI}/manager-client.key",
                        "ca_file": f"{PKI}/ca.crt", "server_name": COMPUTE_NAME},
            },
            "dr": {
                "enabled": self.config["dr-enabled"],
                "run_deadline": self.config["dr-run-deadline"],
                "probe": {
                    "image": self.config["dr-probe-image"],
                    "flavor": self.config["dr-probe-flavor"],
                    "direct": self.config["dr-probe-direct"],
                },
            },
        }
        if self.config["kms-driver"] == "barbican":
            manager["kms"]["barbican"] = {"secret_id": self.config["barbican-secret-id"]}
        console, rebranded = self._console()
        if console:
            manager["console"] = console
        if rebranded:
            changed.add("manager")
        if ceph:
            manager["ceph"] = {
                "conf": "/etc/ceph/ceph.conf", "user": user,
                "volume_pools": _csv(self.config["volume-pools"]),
                "vm_pools": _csv(self.config["vm-pools"]),
                "remove_nova_orphans": bool(self.config["remove-nova-orphans"]),
                "volume_name_template": self.config["volume-name-template"],
                "mover": {"user": mover_user, "keyring": f"/etc/ceph/ceph.client.{mover_user}.keyring"},
                "restore": {"user": restore_user, "keyring": f"/etc/ceph/ceph.client.{restore_user}.keyring"},
                "mirror": {
                    "drive_schedule": self.config["mirror-drive-schedule"],
                    "enable_images": self.config["mirror-enable-images"],
                },
            }
        else:
            manager["ceph"] = {"conf": "", "volume_pools": [], "vm_pools": []}
        mover = {
            "site": site,
            "cloud": self._cloud_name(),
            "manager_url": "https://127.0.0.1:9777",
            "listen": f"{self._admin_host()}:9782",
            "log_level": "info",
            "max_parallel": self.config["mover-max-parallel"],
            "drain_timeout": f"{self._mover_drain()}s",
            "tls": {"client_cert": f"{PKI}/mover.crt", "client_key": f"{PKI}/mover.key", "ca_cert": f"{PKI}/ca.crt"},
            "compute": {"enabled": self.config["local-disk-backup"], "server_name": COMPUTE_NAME, "timeout": "5m"},
            "gc_temp_dir": "/var/lib/ferry/gc",
        }
        if ceph:
            mover["readable_pools"] = sorted(set(
                _csv(self.config["volume-pools"]) + _csv(self.config["vm-pools"]) + _csv(self.config["image-pools"])))
            mover["writable_pools"] = []
        dsn = f"{db['user']}:{db['password']}@tcp({db['endpoint']})/{DB_NAME}?tls=false"
        env = {
            "FERRY_DB_DSN": dsn,
            "FERRY_KEYSTONE_PASSWORD": ks["password"],
            "FERRY_KEYSTONE_APPLICATION_CREDENTIAL_SECRET": p["appcred-secret"],
            "FERRY_NODE_ID": self.unit.name.replace("/", "-"),
        }
        if _write(f"{ETC}/manager.yaml", _yaml(manager) + "\n"):
            changed.add("manager")
        if _write(f"{ETC}/manager.env", "".join(f"{k}={v}\n" for k, v in sorted(env.items())), 0o600):
            changed.add("manager")
        if _write(f"{ETC}/mover.yaml", _yaml(mover) + "\n"):
            changed.add("mover")
        if _write(f"{ETC}/mover.env", f"FERRY_MOVER_ID={self._mover_id(self.unit)}\nFERRY_MOVER_CONFIG=/etc/ferry/mover.yaml\n", 0o600):
            changed.add("mover")
        for component, cmd, mounts in (
            ("manager", "--config=/etc/ferry/manager.yaml", [f"{ETC}/manager.yaml:/etc/ferry/manager.yaml:ro", f"{CONSOLE}:{CONSOLE}:ro"]),
            ("mover", "", [f"{ETC}/mover.yaml:/etc/ferry/mover.yaml:ro", f"{STATE}/mover:/var/lib/ferry"]),
        ):
            entry = ["--entrypoint", "/usr/local/bin/ferry-mover"] if component == "mover" else []
            args = [
                "/usr/bin/podman", "run", "--rm", f"--name=ferry-{component}", "--network=host", "--pull=missing",
                "--log-driver=none",
                f"--env-file={ETC}/{component}.env",
                f"-v={PKI}:{PKI}:ro", f"-v={CEPH}:/etc/ceph:ro",
            ] + [f"-v={m}" for m in mounts] + entry + [self._image(component)] + ([cmd] if cmd else [])
            stop = self._mover_drain() + 60 if component == "mover" else 30
            unit = "\n".join([
                "[Unit]",
                f"Description=Ferry {component}",
                "After=network-online.target",
                "Wants=network-online.target",
                "",
                "[Service]",
                "Restart=always",
                "RestartSec=5",
                f"ExecStartPre=-/usr/bin/podman rm -f ferry-{component}",
                "ExecStart=" + " ".join(args),
                f"ExecStop=/usr/bin/podman stop -t {stop} ferry-{component}",
                f"TimeoutStopSec={stop + 30}",
                "",
                "[Install]",
                "WantedBy=multi-user.target",
                "",
            ])
            if _write(f"/etc/systemd/system/ferry-{component}.service", unit):
                changed.add(component)
                changed.add("daemon-reload")
        return changed

    def _run_services(self, changed):
        if "daemon-reload" in changed:
            subprocess.run(["systemctl", "daemon-reload"], check=True)
        for component in ("manager", "mover"):
            unit = f"ferry-{component}.service"
            subprocess.run(["systemctl", "enable", unit], check=True, capture_output=True)
            action = "restart" if component in changed else "start"
            subprocess.run(["systemctl", action, unit], check=True)

    def _require_healthy(self):
        deadline = time.monotonic() + 90
        while True:
            try:
                with urllib.request.urlopen("http://127.0.0.1:9776/healthz", timeout=5) as r:
                    if r.status == 200:
                        return
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise Waiting("ferry-manager is not answering yet; see journalctl -u ferry-manager")
            time.sleep(3)

    def _publish_compute(self):
        data = self._peers.data[self.app]
        for rel in self.model.relations["compute"]:
            rel.data[self.app].update({
                "ca": data["ca"],
                "cert": data["compute"],
                "key": data["compute-key"],
                "server-name": COMPUTE_NAME,
                "client-cns": json.dumps([MANAGER_CLIENT_CN] + (["ferry-mover-*"] if self.config["local-disk-backup"] else [])),
                "port": str(self.config["compute-port"]),
                "image": self._image("compute"),
                "local-disk-backup": str(self.config["local-disk-backup"]).lower(),
                "expose-metrics": str(self.config["expose-metrics"]).lower(),
            })


if __name__ == "__main__":
    ops.main(FerryCharm)
