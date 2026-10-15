import getpass
import json

import pytest
from ops import testing

import charm

MODEL = testing.Model(name="openstack", uuid="5d4c0b16-5f43-4b0c-9a3e-6b1f1f0e2a11")
METRICS_NETWORK = testing.Network("metrics-endpoint", [testing.BindAddress([testing.Address("192.0.2.20")])])
IDENTITY = {
    "ca": "ca", "cert": "cert", "key": "key", "server-name": "ferry-compute",
    "client-cns": json.dumps(["ferry-manager-client"]), "port": "9779", "image": "ferry-compute:test",
}


@pytest.fixture
def ctx():
    return testing.Context(charm.FerryComputeCharm)


@pytest.fixture
def config(tmp_path):
    return {
        "scratch-dir": str(tmp_path / "libvirt" / "scratch"),
        "qemu-user": getpass.getuser(),
        "libvirt-socket": str(tmp_path / "absent.sock"),
    }


@pytest.fixture
def written(monkeypatch, tmp_path):
    files = {}

    def record(path, content, mode=0o644):
        files[path] = content
        return False

    monkeypatch.setattr(charm, "_write", record)
    monkeypatch.setattr(charm, "PKI", str(tmp_path / "pki"))
    monkeypatch.setattr(charm, "STATE", str(tmp_path / "state"))
    (tmp_path / "pki").mkdir()
    return files


def manager(expose):
    data = dict(IDENTITY, **({"expose-metrics": "true"} if expose else {}))
    return testing.Relation("manager", remote_app_name="ferry", remote_app_data=data)


def state(config, expose, *relations, leader=False):
    return testing.State(
        model=MODEL, networks={METRICS_NETWORK}, config=config, leader=leader,
        relations={manager(expose), *relations},
    )


def admin_bind(ctx, config, written, expose):
    with ctx(ctx.on.start(), state(config, expose)) as mgr:
        mgr.charm._render(mgr.charm._manager(), charm.Layout(mgr.charm.config))
    return json.loads(written[f"{charm.ETC}/compute.yaml"])["admin"]["bind"]


def relate_prometheus(ctx, config, expose):
    rel = testing.Relation("metrics-endpoint", remote_app_name="prometheus")
    out = ctx.run(ctx.on.relation_joined(rel, remote_unit=0), state(config, expose, rel, leader=True))
    return out.get_relation(rel.id), out


def test_admin_listener_stays_on_loopback_by_default(ctx, config, written):
    assert admin_bind(ctx, config, written, expose=False) == "127.0.0.1:9780"


def test_expose_metrics_on_the_ferry_application_binds_the_agent_on_the_metrics_endpoint_address(ctx, config, written):
    assert admin_bind(ctx, config, written, expose=True) == "192.0.2.20:9780"


def test_the_health_check_follows_the_admin_listener(ctx, config, monkeypatch):
    urls = []

    class Answer:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(url, timeout):
        urls.append(url)
        return Answer()

    monkeypatch.setattr(charm.urllib.request, "urlopen", urlopen)
    with ctx(ctx.on.start(), state(config, True)) as mgr:
        mgr.charm._require_healthy(mgr.charm._admin_host(mgr.charm._manager()))
    assert urls == ["http://192.0.2.20:9780/healthz"]


def test_metrics_endpoint_has_nothing_to_scrape_until_exposed(ctx, config):
    rel, _ = relate_prometheus(ctx, config, expose=False)
    assert json.loads(rel.local_app_data["scrape_jobs"]) == []
    assert rel.local_unit_data["prometheus_scrape_unit_address"] == "192.0.2.20"


def test_metrics_endpoint_carries_the_agent_job_and_leaves_the_rules_to_ferry(ctx, config):
    rel, out = relate_prometheus(ctx, config, expose=True)
    assert json.loads(rel.local_app_data["scrape_jobs"]) == [{
        "job_name": "ferry_compute",
        "metrics_path": "/metrics",
        "static_configs": [{"targets": ["*:9780"]}],
        "relabel_configs": [{"target_label": "job", "replacement": "ferry_compute"}],
    }]
    assert json.loads(rel.local_app_data["alert_rules"]) == {}
    assert json.loads(rel.local_app_data["scrape_metadata"]) == {
        "model": "openstack", "model_uuid": MODEL.uuid, "application": "ferry-compute",
        "unit": "ferry-compute/0", "charm_name": "ferry-compute",
    }
    assert rel.local_unit_data["prometheus_scrape_unit_address"] == "192.0.2.20"
    assert rel.local_unit_data["prometheus_scrape_unit_name"] == "ferry-compute/0"
    assert out.unit_status == testing.WaitingStatus(f"no libvirt socket at {config['libvirt-socket']}")
