# Where Ferry deploys

Ferry needs the same five things from every OpenStack: a MariaDB 10.6+ or MySQL
8.0.16+ database, a Keystone service user, the cloud's Ceph (where it has one),
a private CA for its two mTLS relationships, and an agent on every hypervisor.
What differs between distributions is only who provides each, and each
deployer below takes them from the distribution's own mechanism rather than
bringing its own.

| distribution | deploy with | database | Keystone | hypervisor agent | verified on |
|---|---|---|---|---|---|
| kolla-ansible | [`kolla-ansible`](../kolla-ansible/README.md) | kolla's Galera, via proxysql | `service-ks-register` | container on every compute host | lab and production |
| openstack-helm | [`helm/ferry`](../helm/ferry/README.md), `presets/openstack-helm.yaml` | OSH's `mariadb` | `ferry-manager bootstrap` with `keystone-keystone-admin` | DaemonSet | OSH 2025.1 lab |
| RHOSO 18 / openstack-k8s-operators | `helm/ferry`, `presets/rhoso.yaml` | Ferry's own MariaDB 11.4 (the platform's Galera is 10.5) | `KeystoneService` CR | `OpenStackDataPlaneService` on EDPM | RHOSO 18.0 lab |
| Atmosphere, Genestack, StarlingX, MOSK, Yaook | `helm/ferry`, a preset each | as the preset names | `ferry-manager bootstrap` | DaemonSet | not in a lab |
| Charmed OpenStack | [`juju`](../juju/README.md) | mysql-innodb-cluster 8.0, via `mysql-router` | `identity-credentials` | `ferry-compute` subordinate to `nova-compute` | Charmed 2024.1 single-VM lab |
| Canonical OpenStack (Sunbeam) | `juju` | mysql-k8s 8.0, via a `mysql-router-k8s` offer | `keystone-credentials` offer | `ferry-compute` subordinate to `openstack-hypervisor` | Sunbeam 2024.1 lab |

## What every deployer gets for free from the manager

Five things used to be kolla's job and are now the manager's own, so a new
deployer does not have to reinvent them:

- **The database dialect.** The migrations are written for MariaDB and are
  immutable once applied. On MySQL the manager resolves each `IF [NOT] EXISTS`
  clause against `information_schema` as it applies it, and replicas that start
  together wait for one another's migrations instead of failing on a column the
  other has just added.
- **The application credential.** Give the manager its service user's password
  and a secret, and it finds or creates its own restricted credential by name
  (`keystone.application_credential_name`) — reused by every replica, replaced if
  its secret moves.
- **The catalog entry.** `keystone.register_in_catalog` keeps Ferry's `backup`
  entry asserted, which is how it appears on distributions whose Keystone
  integration does not know the service.
- **Where the agents are.** `compute.hosts_from_nova` dials each hypervisor's
  agent at the `host_ip` Nova reports, so a node added later is reachable
  without re-rendering anything.
- **Certificate renewal.** Managers, movers and agents re-read their
  certificates when the files change, within thirty seconds, so cert-manager or
  a charm leader can renew a leaf without anyone restarting anything.

`ferry-manager bootstrap --database --keystone` creates the database, its
account, and the service user with its grants, idempotently, for deployers that
have an admin credential and no native mechanism for either.

## Clouds that are not Ceph end to end

An empty `ceph.conf` means the cloud has no cluster, and a catalog with no block
storage service means it has no Cinder. Both are supported: instances are then
protected through their hypervisors, which is the only way to reach a local
Nova root anyway.

**A local Nova root is restored through the agent too**, in place or as a new
instance, whatever Nova's `images_type`. The default on every distribution here
is qcow2 over a `_base` image, which the agent writes through `qemu-nbd` (see
Local disks); the agent therefore needs Nova's instances
directory, which each deployer mounts.

## What the labs proved

| | openstack-helm 2025.1 | RHOSO 18.0 | Charmed 2024.1 | Sunbeam 2024.1 |
|---|---|---|---|---|
| volume backup, restore, bytes compared | yes | no Cinder | yes | yes |
| instance backup through the agent | yes | yes, SELinux enforcing | yes, AppArmor | yes, the snap's libvirt |
| local qcow2 root restored in place, bytes compared | yes | yes | yes | yes |
| local root restored as a new instance, boots | yes | — | — | — |
| guest stopped and started after a backup | yes | yes | yes | yes |
| certificates renewed without a restart | yes, cert-manager | — | — | — |
| cephx split into three keys | yes, by the chart's Job | no Ceph | yes | yes |

The byte comparison for a local root hashes the guest's view of the disk
(`qemu-img convert` of the overlay) while the instance is paused, backs it up,
stops it — a shutdown writes to the disk — restores in place and hashes again.
