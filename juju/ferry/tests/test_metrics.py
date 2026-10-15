import json
import shutil
from pathlib import Path

import pytest
import yaml
from ops import testing

import charm

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / charm.ALERT_RULES
MODEL = testing.Model(name="openstack", uuid="5d4c0b16-5f43-4b0c-9a3e-6b1f1f0e2a11")
METRICS_NETWORK = testing.Network("metrics-endpoint", [testing.BindAddress([testing.Address("192.0.2.10")])])
PKI = {k: k for k in (
    "ca", "mover-api", "mover-api-key", "manager-client", "manager-client-key", "mover", "mover-key", "appcred-secret")}
DB = {"user": "ferry", "password": "pw", "endpoint": "db:3306"}
KS = {
    "auth_url": "http://keystone:5000/v3", "username": "ferry", "password": "pw", "project": "services",
    "user_domain": "service_domain", "project_domain": "service_domain", "region": "RegionOne",
}


@pytest.fixture
def ctx(tmp_path):
    (tmp_path / charm.ALERT_RULES).parent.mkdir(parents=True)
    shutil.copy(RULES, tmp_path / charm.ALERT_RULES)
    return testing.Context(charm.FerryCharm, charm_root=tmp_path)


@pytest.fixture
def written(monkeypatch, tmp_path):
    files = {}

    def record(path, content, mode=0o644):
        files[path] = content
        return False

    monkeypatch.setattr(charm, "_write", record)
    monkeypatch.setattr(charm, "CONSOLE", str(tmp_path / "console"))
    return files


def state(**kwargs):
    return testing.State(model=MODEL, networks={METRICS_NETWORK}, **kwargs)


def render(ctx, written, **config):
    with ctx(ctx.on.start(), state(config=config)) as mgr:
        mgr.charm._render(DB, KS, None, PKI)
    return json.loads(written[f"{charm.ETC}/manager.yaml"]), json.loads(written[f"{charm.ETC}/mover.yaml"])


def relate_prometheus(ctx, leader=True, **config):
    rel = testing.Relation("metrics-endpoint", remote_app_name="prometheus")
    out = ctx.run(ctx.on.relation_joined(rel, remote_unit=0), state(leader=leader, relations={rel}, config=config))
    return out.get_relation(rel.id), out


def test_admin_listeners_stay_on_loopback_by_default(ctx, written):
    manager, mover = render(ctx, written)
    assert manager["admin"]["bind"] == "127.0.0.1:9775"
    assert mover["listen"] == "127.0.0.1:9782"


def test_expose_metrics_binds_the_admin_listeners_on_the_metrics_endpoint_address(ctx, written):
    manager, mover = render(ctx, written, **{"expose-metrics": True})
    assert manager["admin"]["bind"] == "192.0.2.10:9775"
    assert mover["listen"] == "192.0.2.10:9782"


def test_metrics_endpoint_has_nothing_to_scrape_until_exposed(ctx):
    rel, _ = relate_prometheus(ctx)
    assert json.loads(rel.local_app_data["scrape_jobs"]) == []
    assert json.loads(rel.local_app_data["alert_rules"]) == {}
    assert rel.local_unit_data["prometheus_scrape_unit_address"] == "192.0.2.10"


def test_metrics_endpoint_carries_the_manager_and_mover_jobs_and_the_kolla_rules(ctx):
    rel, out = relate_prometheus(ctx, **{"expose-metrics": True})
    jobs = {j["job_name"]: j for j in json.loads(rel.local_app_data["scrape_jobs"])}
    assert set(jobs) == {"ferry_manager", "ferry_mover"}
    assert jobs["ferry_manager"]["static_configs"] == [{"targets": ["*:9775"]}]
    assert jobs["ferry_mover"]["static_configs"] == [{"targets": ["*:9782"]}]
    for name, job in jobs.items():
        assert job["metrics_path"] == "/metrics"
        assert job["relabel_configs"] == [{"target_label": "job", "replacement": name}]
        assert job["metric_relabel_configs"] == [{"action": "labeldrop", "regex": "juju_unit"}]
    rules = json.loads(rel.local_app_data["alert_rules"])
    assert rules == yaml.safe_load(RULES.read_text())
    assert {"FerryTargetDown", "FerryReferencedObjectMissing"} <= {r["alert"] for r in rules["groups"][0]["rules"]}
    assert json.loads(rel.local_app_data["scrape_metadata"]) == {
        "model": "openstack", "model_uuid": MODEL.uuid, "application": "ferry", "unit": "ferry/0", "charm_name": "ferry",
    }
    assert rel.local_unit_data["prometheus_scrape_unit_address"] == "192.0.2.10"
    assert rel.local_unit_data["prometheus_scrape_unit_name"] == "ferry/0"
    assert out.unit_status == testing.WaitingStatus("waiting for the peer relation")


def test_a_follower_publishes_only_its_own_address(ctx):
    rel, _ = relate_prometheus(ctx, leader=False, **{"expose-metrics": True})
    assert "scrape_jobs" not in rel.local_app_data
    assert rel.local_unit_data["prometheus_scrape_unit_address"] == "192.0.2.10"


def test_compute_agents_learn_expose_metrics_from_the_leader(ctx):
    peers = testing.PeerRelation("peers", local_app_data={"ca": "ca", "compute": "cert", "compute-key": "key"})
    compute = testing.Relation("compute", remote_app_name="ferry-compute")
    with ctx(ctx.on.start(), state(leader=True, relations={peers, compute}, config={"expose-metrics": True})) as mgr:
        mgr.charm._publish_compute()
        out = mgr.run()
    assert out.get_relation(compute.id).local_app_data["expose-metrics"] == "true"
