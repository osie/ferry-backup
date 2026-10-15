# Ferry on Juju: Charmed OpenStack and Canonical OpenStack (Sunbeam)

Two machine charms serve both Juju-driven distributions.

| charm | kind | what it runs |
|---|---|---|
| `ferry` | principal | `ferry-manager` and `ferry-mover`, as podman containers with host networking |
| `ferry-compute` | subordinate | `ferry-compute` on the hypervisor it is attached to |

Machine charms rather than Kubernetes ones, and on purpose. Charmed OpenStack is
machines all the way down, and its `mysql-router` listens on the principal's own
`127.0.0.1` only, so the manager has to share a machine with it. Sunbeam's
control plane is on Kubernetes, but the machine model is where Sunbeam itself
puts `cinder-volume`, consuming the database, Keystone and Ceph through the same
cross-model offers `ferry` uses. One charm then covers both, and every relation
it speaks is the platform's own.

## What each relation carries

| endpoint | Charmed OpenStack | Sunbeam |
|---|---|---|
| database | `shared-db` to a `mysql-router` subordinate beside `ferry` | `database` to a `mysql-router-k8s` offered with `expose-external=loadbalancer` |
| identity | `identity-credentials` to `keystone` | `identity-credentials` to the `keystone-credentials` offer |
| ceph | `ceph` to `ceph-mon:client` | `ceph` to `microceph:ceph` |
| hypervisor | `ferry-compute` subordinate to `nova-compute` | `ferry-compute` subordinate to `openstack-hypervisor` |

Both databases are MySQL 8.0 with Group Replication, which the manager supports
from 8.0.16. Neither Keystone charm registers a catalog entry for a service it
does not know, so `ferry` asks only for a user, registers its own `backup`
entry, and mints its own restricted application credential from that user's
password.

**Three cephx keys, with a deliberate privilege split.** ceph-mon and microceph name the key they
issue after the `application-name` a relation sends, so `ferry` asks for one per
relation: `client.ferry` on `ceph` for the manager, `client.ferry-mover` on
`ceph-mover`, read-only on every pool, and `client.ferry-restore` on
`ceph-restore`, read-write on the volume and Nova pools and read-only on the image
pools. Each is narrowed by its own `set-key-permissions` broker request, sent
again once the key exists: microceph answers the first request before it has
created the key, so its `auth caps` fails, is logged, and the broker still
answers success. The two extra relations are optional; without them the mover
and restores fall back to `client.ferry` and the manager says at startup that
the split is not in force. All three keyrings land in `/etc/ceph` on the unit,
where both the manager and the mover read them: the manager writes as the
restore identity itself when it seals a standby, promotes one, or clones an
instant restore.

**An instant tier needs Cinder's key to read it.** An instant restore is a clone
of a snapshot in the RBD tier pool that Cinder adopts, and until Ferry's
background flatten finishes, every read of the restored volume follows the
clone into the tier: Cinder's when it adopts it, Nova's when it attaches it.
Neither the `ferry` charm nor the Ceph broker grants Cinder's key anything on a
pool Ferry created. That key is `client.cinder-ceph` on Charmed OpenStack, which nova-compute
also attaches with through `ceph-access`, and on Sunbeam the key `cinder-volume`'s
Ceph relation was issued (`ceph auth ls` names it). Add
`profile rbd-read-only pool=<tier pool>` to it by hand, keeping its existing
caps: `ceph auth caps` replaces the whole list.
Without it every instant restore fails with `Cinder could not open the clone;
its Ceph identity needs read access to tier pool <pool> until the flatten
finishes`.

**So does a warm standby pool.** A standby promote, and a warm group's recover
and test, clone a standby's applied snapshot out of its pool and hand the clone
to Cinder the same way, so the same key needs `profile rbd-read-only
pool=<standby pool>` added by hand as well: until a promote's or a recover's
flatten finishes, and for the whole life of a test's clones, which are never
flattened. Without it a warm recover or test fails at materialise with `Cinder
could not open the clone; its Ceph identity needs read access to standby pool
<pool>`.

**The compute agents share one server identity**, handed to every
`ferry-compute` unit by the `ferry` leader and checked by name. The manager
finds each agent at the `host_ip` Nova reports for its hypervisor.

**A mover that is restarted hands its jobs back.** `mover-drain-timeout` (90
seconds) is how long a stopping mover lets its jobs finish; whatever is still
running then is handed back and resumes on another mover from what it already
wrote, without spending an attempt. The mover's unit stops the container with
`podman stop -t` the drain plus a minute, and gives systemd's `TimeoutStopSec`
half a minute more, so neither kills the mover mid-drain.

## Charmed OpenStack

```sh
juju deploy ./ferry_amd64.charm ferry \
  --config cloud-name=<cloud> \
  --config volume-pools=cinder-ceph --config image-pools=glance --config vm-pools=
juju deploy mysql-router ferry-mysql-router --channel 8.0/stable
juju integrate ferry:shared-db ferry-mysql-router:shared-db
juju integrate ferry-mysql-router:db-router mysql-innodb-cluster:db-router
juju integrate ferry:identity-credentials keystone:identity-credentials
juju integrate ferry:ceph ceph-mon:client
juju integrate ferry:ceph-mover ceph-mon:client
juju integrate ferry:ceph-restore ceph-mon:client

juju deploy ./ferry-compute_amd64.charm ferry-compute
juju integrate ferry-compute:juju-info nova-compute:juju-info
juju integrate ferry-compute:manager ferry:compute
```

`ferry` ships an LXD profile with `security.nesting`, so it can be placed in a
container (`--to lxd:<machine>`), which is where Charmed OpenStack puts its API
services. `vm-pools` stays empty unless Nova's `libvirt-image-backend` is `rbd`.

**With `vm-pools` set, deleting an instance whose disk Ferry protects leaks that
disk unless `remove-nova-orphans` is true.** Nova's delete gives up on a
`<uuid>_disk` that carries Ferry's anchor snapshot and reports the instance
deleted anyway, so the disk stays in the pool at full size and nothing in
OpenStack names it. The manager finds it, drops the anchor and counts it in
`leaked_disk_bytes` (`ferry_manager_leaked_disk_bytes` in Prometheus); with
the option on it also deletes it, once Nova answers 404 for the instance and the
disk has no snapshot or clone of anyone else's. Off by default, because deleting a
tenant's disk on the strength of an instance listing is the operator's call. The
anchor is dropped once Ferry stops protecting the disk (the instance leaves its
plan, or a deleted plan's grace ends), so the exposure is a disk deleted while
protected or inside that grace.
`juju config ferry remove-nova-orphans=true` removes the disks already leaked on
the next reconcile pass over the Nova pool, one log line each; left off, remove
them with `rbd rm` once `openstack server show <uuid>` answers "No server", and
the manager stops counting them on its next pass.

The keystone charm names its admin role `Admin` and grants it on projects and
the `admin_domain`, never on the system. Ferry matches role names without regard
to case, as oslo.policy does, but its operator surface wants a system-scoped
token, so an operator needs the assignment once:

```sh
openstack role add --system all --user admin --user-domain admin_domain Admin
```

Nova's QEMU runs under a per-guest AppArmor profile that knows only the paths
libvirt generated for it, which leaves out the sockets and scratch a pull-mode
backup needs. `ferry-compute` adds both directories through
`/etc/apparmor.d/abstractions/libvirt-qemu.d/ferry-compute`, the drop-in the
Ubuntu abstraction includes, and reloads the profiles of guests already
running, since a profile only reads its abstractions when it is loaded.
Removing the unit takes the drop-in away again.

## Canonical OpenStack (Sunbeam)

```sh
juju deploy -m openstack mysql-router-k8s ferry-mysql-router --channel 8.0/stable --trust \
  --config expose-external=loadbalancer
juju integrate -m openstack ferry-mysql-router:backend-database mysql:database
juju offer openstack.ferry-mysql-router:database ferry-mysql-router

M=admin/openstack-machines
juju consume -m $M <owner>/openstack.ferry-mysql-router
juju deploy -m $M ./ferry_amd64.charm ferry --to <machine> \
  --config cloud-name=<cloud> \
  --config volume-pools=cinder-volume-ceph --config image-pools=glance --config vm-pools=
juju integrate -m $M ferry:database ferry-mysql-router
juju integrate -m $M ferry:identity-credentials keystone-credentials
juju integrate -m $M ferry:ceph microceph:ceph
juju integrate -m $M ferry:ceph-mover microceph:ceph
juju integrate -m $M ferry:ceph-restore microceph:ceph

juju deploy -m $M ./ferry-compute_amd64.charm ferry-compute
juju integrate -m $M ferry-compute:juju-info openstack-hypervisor:juju-info
juju integrate -m $M ferry-compute:manager ferry:compute
```

Sunbeam's Nova keeps instance disks local, so `ferry-compute` is what reads
them. It recognises the `openstack-hypervisor` snap and puts its scratch under
`/var/snap/openstack-hypervisor/common/`, the one host path the snap's QEMU can
open, and talks to the snap's libvirt socket there.

## Images

The charms pull `<registry>/ferry-{manager,mover,compute}:<version>`. For a
cloud that cannot reach the registry, attach `docker save` archives as the
`manager-image`, `mover-image` and `compute-image` resources.

An attached archive must hold the image `registry` and `version` name. When
it holds another, the unit leaves its running services and their config
alone and goes blocked, naming the image it holds and the one it would run,
until the two agree: set `version` to the archive's tag, or attach an empty
file as the resource to pull from the registry again. A refresh with new
archives therefore restarts nothing until `version` names them:

```sh
juju refresh ferry --path=./ferry_amd64.charm \
  --resource manager-image=./manager.tar --resource mover-image=./mover.tar
juju config ferry version=<the archives' tag>
```

The order does not matter. Set `version` first and the unit waits, blocked,
for the refresh that brings its image. Either way each service restarts once,
on the new image with the config rendered for it. `ferry-compute` compares
its `compute-image` with the image the `ferry` application names, so it
switches in the same step.

## Branding the console

The `console-*` options put your name, colour and logo on the console; the
logo, dark logo, favicon and stylesheet are given base64-encoded. See
[docs/console-branding.md](../docs/console-branding.md#juju).

## Monitoring with COS

Every Ferry process serves `/metrics` on an admin port: 9775 for the manager,
9782 for the mover, 9780 for each `ferry-compute` agent. They listen on loopback
by default, because `/metrics` carries per-project quota figures and the same
listener serves `/loglevel`. One option on `ferry` changes that for all three,
and both charms provide `metrics-endpoint` over `prometheus_scrape`:

```sh
juju config ferry expose-metrics=true
juju integrate ferry:metrics-endpoint <prometheus>
juju integrate ferry-compute:metrics-endpoint <prometheus>
```

`<prometheus>` is COS's `prometheus` application, or a cross-model offer of its
`metrics-endpoint`. With the option set, each unit binds its admin listeners on
the address of its own `metrics-endpoint` binding, and `ferry` hands the setting
to its `ferry-compute` units over `compute`. Deploy with
`--bind metrics-endpoint=<space>` to keep that address on a network only
operators and Prometheus reach.

Prometheus then scrapes every unit as `ferry_manager`, `ferry_mover` and
`ferry_compute`, the job names the kolla role uses, and `ferry` hands over
`ferry.rules`, the fourteen alert rules the kolla role installs, unchanged.
They are not scoped to one application, so they cover the agents too. The
manager's and mover's series drop `juju_unit`, which `instance` still carries,
so the rules aggregate across units as they do on kolla. Until `expose-metrics`
is set the relation offers nothing to scrape, and the units' status says so.

## Building

```sh
charmcraft pack
```

On an Ubuntu 24.04 host without LXD, `charmcraft pack --destructive-mode`.

Each charm's unit tests run from its own directory:

```sh
pip install 'ops[testing]' pytest
python3 -m pytest tests
```

## A test cloud on one machine

[`bundles/charmed-openstack-lab.yaml`](bundles/charmed-openstack-lab.yaml) is a
throwaway Charmed OpenStack 2024.1 on two LXD virtual machines, with Ceph, Cinder
and Nova, sized to try Ferry on one host:

```sh
juju add-model ferry-lab
juju deploy ./bundles/charmed-openstack-lab.yaml
```

Then deploy and relate `ferry` and `ferry-compute` as in
[Charmed OpenStack](#charmed-openstack). Its Keystone admin password is the
well-known `openstack`, so never point it at anything real.

## Verified

| | Charmed OpenStack 2024.1 | Sunbeam 2024.1 |
|---|---|---|
| database | mysql-innodb-cluster 8.0 via mysql-router | mysql-k8s 8.0.44 via mysql-router-k8s |
| Ceph | ceph-mon squid, `cinder-ceph`, three keys | microceph, three keys |
| volume backup and restore, compared byte for byte | yes | yes |
| instance backup through the hypervisor | yes, QEMU under AppArmor | yes, through the snap's libvirt |
| local qcow2 root restored in place, compared byte for byte | yes | yes |
| guest stopped and started after a backup | yes | yes |
| `juju refresh` of both charms onto a new image | yes | yes |

Nova 2024.1 undefines a guest to start it after a stop, and libvirt refuses that
while a checkpoint is defined, so on these clouds a guest the agent had backed up
could not be started. The agent now keeps no checkpoint in libvirt between
backups.
