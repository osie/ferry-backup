import json

from test_metrics import DB, KS, PKI, ctx, state, written

import charm

CEPH = {"mons": ["192.0.2.20"], "keys": {"ferry": "AQBkey=="}}

__all__ = ["ctx", "written"]


def manager_ceph(ctx, written, **config):
    with ctx(ctx.on.start(), state(config=config)) as mgr:
        mgr.charm._render(DB, KS, CEPH, PKI)
    return json.loads(written[f"{charm.ETC}/manager.yaml"])["ceph"]


def test_leaked_nova_disks_stay_unless_the_operator_asks(ctx, written):
    assert manager_ceph(ctx, written)["remove_nova_orphans"] is False


def test_remove_nova_orphans_reaches_the_manager(ctx, written):
    assert manager_ceph(ctx, written, **{"remove-nova-orphans": True})["remove_nova_orphans"] is True
