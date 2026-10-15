# Ferry on Kubernetes-hosted OpenStack

One chart for every OpenStack whose control plane runs on Kubernetes:
openstack-helm and the distributions built on it (Atmosphere, Genestack,
StarlingX, Mirantis MOSK), Yaook, and Red Hat OpenStack Services on OpenShift.
What differs between them is where the database, the Keystone admin, the Ceph
configuration and libvirt live, and each difference is a value. `presets/` holds
those values for each distribution.

```sh
helm upgrade --install ferry helm/ferry -n openstack \
  -f helm/ferry/presets/openstack-helm.yaml \
  --set cloud.name=<this cloud>
```

## What it deploys

| workload | kind | what it is |
|---|---|---|
| `<release>-manager` | Deployment | API, scheduler, conductor. Owns the database and the Keystone credential |
| `<release>-mover` | StatefulSet | data plane. Reads Ceph, writes the repository. No database credential, no OpenStack credential |
| `<release>-compute` | DaemonSet | on every hypervisor: fsfreeze through libvirt, and optionally the local-disk read path |
| `<release>-mariadb` | StatefulSet | only with `database.embedded.enabled`, for a cloud whose own database cannot hold Ferry |

The manager is registered in the cloud's catalog by the manager itself
(`keystone.catalog.register`), under service type `backup`. It mints its own
restricted application credential on first start from the service user's
password and a secret the chart generates, so no job has to hand one over.

A `helm upgrade` rolls the managers, the movers and the compute agents at once,
so each mover pod starts with `ferry-mover await-managers`, which waits until
every manager pod behind `<release>-manager-replicas` runs the mover's release or
a newer one. A build without a plain release number (a lab build such as
`0.14.0-lab3`, or `dev`) waits for managers reporting exactly the same version.
`mover.awaitManagers: false` skips it. Compute pods wait the same
way with `ferry-compute await-managers`, which asks each manager's `/healthz` on
the plain API port, since the agent holds no client certificate;
`compute.awaitManagers: false` skips it.

## Before you install

**Install into the namespace the rest of OpenStack lives in.** The chart reads
the platform's own Secrets and ConfigMaps (the database admin password, the
Keystone admin credentials, `ceph.conf`, the Ceph admin key), and Kubernetes does
not mount those across namespaces. On openstack-helm that is `openstack`.

**cert-manager must be installed.** Ferry's two mTLS relationships (mover to
manager, manager and mover to the compute agents) use a private CA the chart
creates as a cert-manager `Issuer`. It is deliberately not the platform's CA: a CA
that signs other components' certificates would let them present a Ferry
identity. Set `pki.certManager.issuerRef` to use one of your own anyway, or
`pki.provider: existing` to supply every certificate yourself.

**The database must be MariaDB 10.6+ or MySQL 8.0.16+.** The manager refuses
anything older at startup, by name. RHOSO's Galera is 10.5, which is why its
preset turns on `database.embedded`.

## What the chart creates, and what it reads

It creates, idempotently, on every rollout, from an init container of the
manager pod (`ferry-manager bootstrap`):

- the database and its account, when `database.bootstrap.enabled`, using the
  admin account named in `database.bootstrap.admin`;
- the Keystone service user, its project and its role grants (`admin` and
  `reader` on `service`), when `keystone.bootstrap.enabled`, using the Keystone
  admin whose `OS_*` variables are in the Secret `keystone.bootstrap.adminSecret`.

The admin credentials reach only that init container. The manager container
never sees them.

With `ceph.keyrings.bootstrap.enabled` a Job creates three cephx identities with
a deliberate privilege split: the manager's, the mover's (read-only on production pools) and
the restore identity (the only one that can write a live volume). It writes
their keyrings into one Secret. It needs the Ceph admin key, and its Role can
create Secrets and touch only that one. Without it, put the three keyrings in a
Secret yourself and name it in `ceph.keyrings.existingSecret`; the keys are
`ceph.client.<user>.keyring`.

The manager pods mount two of the three: their own, and the restore identity's,
because the manager itself writes as the restore identity when it seals a
standby after a replay, promotes one, and clones an instant restore out of the
tier. The movers mount theirs and the restore identity's. A manager without the
restore keyring used to wait for librados to time out and fail every seal with
`context canceled`; it now says which keyring file it could not find.

**An instant tier needs Cinder's key to read it.** An instant restore is a clone
of a snapshot in the tier pool (list it under `ceph.pools.ferry`), and until
Ferry's background flatten finishes every read of the restored volume follows
the clone into that pool: Cinder's, when it adopts the volume, and Nova's,
when it attaches it. The chart does not manage Cinder's cephx key, so grant the
identity Cinder and Nova attach RBD volumes with (`client.cinder` on
openstack-helm) `profile rbd-read-only pool=<tier pool>` alongside the caps it
already has; `ceph auth caps` replaces the whole list. Without it every instant
restore fails with `Cinder could not open the clone; its Ceph identity needs read
access to tier pool <pool> until the flatten finishes`.

**So does a warm standby pool.** A standby promote, and a warm group's recover
and test, clone a standby's applied snapshot out of its pool and hand the clone
to Cinder the same way, so the same identity needs `profile rbd-read-only
pool=<standby pool>` as well: until a promote's or a recover's flatten
finishes, and for the whole life of a test's clones, which are never flattened.
Without it a warm recover or test fails at materialise with `Cinder could not
open the clone; its Ceph identity needs read access to standby pool <pool>`.

**Deleting an instance whose disk Ferry protects leaks that disk unless
`ceph.removeNovaOrphans` is true.** Nova's delete gives up on a `<uuid>_disk`
that carries Ferry's anchor snapshot and reports the instance deleted anyway, so
the disk stays in the Nova pool at full size and nothing in OpenStack names it.
The manager finds it, drops the anchor and counts it in `leaked_disk_bytes`
(`ferry_manager_leaked_disk_bytes` in Prometheus); with the value on it also
deletes it, once Nova answers 404 for the instance and the disk has no snapshot
or clone of anyone else's. Off by default, because deleting a tenant's disk on
the strength of an instance listing is the operator's call. The anchor is dropped
once Ferry stops protecting the disk (the instance leaves its plan, or a deleted
plan's grace ends), so the exposure is a disk deleted while protected or inside
that grace. Turning it on later
removes the disks already leaked on the next reconcile pass over the Nova pool,
one log line each; left off, remove them with `rbd rm` once
`openstack server show <uuid>` answers "No server", and the manager stops counting
them on its next pass.

Passwords are generated into `<release>-credentials` on first install and kept
across upgrades. The chart finds them again with `lookup`, which a renderer that
never talks to the cluster (Argo CD, Flux with `helm template`) does not have.
Create that Secret yourself and set `credentials.existingSecret` there. Its keys
are `database-password`, `keystone-password` and `application-credential-secret`.

## The compute agents

The DaemonSet runs on the host network on every node matching
`compute.nodeSelector`, binds that node's own address, and mounts libvirt's run
directory rather than its socket, so a libvirt restart that replaces the socket
does not leave the agent holding a dead one.

**The manager finds each agent by asking Nova.** It dials the `host_ip` Nova
reports for the hypervisor, so a node added to the cluster is reachable the
moment Nova knows it, without re-rendering anything. `compute.hosts` overrides
that for a hypervisor whose `host_ip` is not the address to use.

**Every agent shares one server identity**, checked by name
(`compute.tlsServerName`) rather than by the address dialled. A per-node
certificate would need every agent to be able to read every other agent's
private key, or a controller minting them, and a DaemonSet has one pod
template. What sharing costs, stated plainly: a compromised hypervisor could
answer as another hypervisor's agent to anything that reaches it. It cannot
become a mover or the manager; its certificate is for server authentication only
and its name is on no client list.

**Where the hypervisors are not Kubernetes nodes** (RHOSO's EDPM), set
`compute.daemonset: false` and `compute.edpm.enabled: true`. The chart then
renders an `OpenStackDataPlaneService` whose playbook runs the agent under
podman and systemd on every node of `compute.edpm.nodeSets`, and an
`OpenStackDataPlaneDeployment` that applies it, as a post-install and
post-upgrade hook so the operator always reads the service as this release
rendered it. The playbook also declares the agent's port to EDPM's nftables,
through the same `/var/lib/edpm-config/firewall/` snippet every other EDPM
service uses, and labels the scratch `svirt_image_t` and the socket directory
`qemu_var_run_t`: SELinux is enforcing there, and QEMU runs as `svirt_t`.

**Certificates renew in place.** cert-manager renews every leaf a month before
its year is up, and the managers, movers and agents notice the changed files
within thirty seconds and present the new certificate on the next connection,
without a restart. On EDPM nodes the files are copies, so a renewal reaches
them with the next `helm upgrade`, which re-runs the deployment.

`compute.localDiskBackup` turns the agent into the read and write path for disks
Ferry cannot reach from the cluster: Nova's local qcow2 roots, which is
openstack-helm's default (`images_type: qcow2`). The scratch it hands QEMU must be
under a path QEMU sees identically: `compute.libvirt.libDir`, which the libvirt
chart mounts from the host. A restore writes the stopped guest's disk itself, a
qcow2 one through the `qemu-nbd` in the agent's image, so the agent also mounts
`compute.novaInstancesDir`, where Nova keeps the disks and their `_base` images.

## Distributions

| preset | database | Keystone admin | Ceph | compute | verified |
|---|---|---|---|---|---|
| `openstack-helm.yaml` | `mariadb` (Galera) | `keystone-keystone-admin` | `ceph-etc`, `pvc-ceph-client-key` | `openstack-compute-node=enabled`, `/run/libvirt/libvirt-sock` | lab, OSH 2025.1 |
| `rhoso.yaml` | embedded (Galera is 10.5) | `KeystoneService` CR | `ceph-conf-files` | EDPM, `OpenStackDataPlaneService` | lab, RHOSO 18.0 |
| `atmosphere.yaml` | Percona XtraDB 8.0 | `keystone-keystone-admin` | `ceph-etc` | OSH libvirt | not in a lab |
| `genestack.yaml` | mariadb-operator 11.8 | `keystone-keystone-admin` | `ceph-etc` | OSH libvirt | not in a lab |
| `starlingx.yaml` | `mariadb` (Galera) 11.4 | `keystone-keystone-admin` | `ceph-etc`, `pvc-ceph-client-key` | OSH libvirt | not in a lab |
| `mosk.yaml` | `mariadb` (Galera) 11.4 | `keystone-keystone-admin` | operator-supplied keyrings | OSH libvirt, QEMU under AppArmor | not in a lab |
| `yaook.yaml` | embedded | an `OS_*` Secret you create | operator-supplied keyrings | `compute.yaook.cloud/hypervisor` | not in a lab |

"Not in a lab" means the preset is built from each distribution's own source and
has not been run against one. Check every name in it against the cloud in front
of you before trusting it.

## Clouds without Ceph, or without Cinder

`ceph.enabled: false` tells the manager there is no cluster: no health probe, no
reconciler, and every RBD-backed disk refused as `no_ceph_connection` rather
than retried. A cloud whose catalog has no block storage service at all is
supported too: the manager says so at startup, protects instances through their
hypervisors, and treats every volume as not found. The RHOSO lab this chart was
verified on is both.

## Not in the chart yet

What the kolla role can do and this chart cannot: file-level recovery (the
`ferry-mover-flr` image and its browse listener), movers in the storage fabric
(privileged, with the host's `/dev` and an initiator identity on a volume each
replica owns), bucket
protection through RGW, and a TLS listener on the manager itself. The API is
plain HTTP inside the cluster and TLS terminates at the ingress or Route.

## Branding the console

`console.*` puts your name, logo and colour on the console; the image files come
from a ConfigMap you create. See
[docs/console-branding.md](../../docs/console-branding.md#helm).

## Monitoring with the Prometheus Operator

Every Ferry pod serves `/metrics` on an admin port: 9775 on a manager, 9782 on a
mover, `compute.adminPort` (9780) on a compute agent. The movers' listener is on
the pod network already, behind `<release>-mover`; the managers' and the agents'
are on loopback, because `/metrics` carries per-project quota figures and the
same listener serves `/loglevel`. Three values, all off by default, hand them to
the Prometheus Operator:

```sh
helm upgrade --install ferry helm/ferry -n openstack ... \
  --set metrics.enabled=true \
  --set metrics.serviceMonitor.enabled=true \
  --set metrics.prometheusRule.enabled=true
```

`metrics.enabled` binds a manager's admin listener on its pod's address and an
agent's on its node's (`status.hostIP`, since the agents are on the host network),
names the port `admin` on both, and adds a headless `<release>-manager-admin`
Service. None of it is put behind an ingress: keep it inside the cluster, with a
NetworkPolicy where the namespace is shared.

`metrics.serviceMonitor.enabled` renders a ServiceMonitor each for the managers
and the movers and a PodMonitor for the agents, and needs `metrics.enabled`. They
set `job` to `ferry_manager`, `ferry_mover` and `ferry_compute`, the names the
kolla role's scrape config uses, and drop `pod` from the managers' and movers'
series so the rules aggregate across replicas as they do on kolla; a manager's
series still name their pod in `node`. kube-prometheus-stack only picks up
monitors and rules carrying its release label by default: put it in
`metrics.serviceMonitor.labels` and `metrics.prometheusRule.labels`.

`metrics.prometheusRule.enabled` renders `files/ferry.rules`, the fourteen alert
rules the kolla role installs, as a PrometheusRule. See
[docs/kolla-ansible.md](../../docs/kolla-ansible.md#monitoring-with-kollas-prometheus)
for what they page on.

Agents on EDPM nodes keep their admin listener on loopback, and nothing here
scrapes them.

## Values

| key | default | what it is |
|---|---|---|
| `cloud.name` | required | this deployment's name, recorded in every point |
| `cloud.site` | `cloud.name` | the locality movers claim work from |
| `database.host` / `port` / `name` / `user` | `mariadb.openstack.svc.cluster.local`, 3306, `ferry`, `ferry` | where the manager's schema lives |
| `database.params` | `tls=false` | go-sql-driver DSN parameters |
| `database.bootstrap.*` | on | create the database and account with the admin named here |
| `database.embedded.*` | off | run a single MariaDB 11.4 for Ferry alone |
| `keystone.authUrl` | OSH's internal URL | Keystone v3 |
| `keystone.user` / `project` / domains / `roles` | `ferry`, `service`, `Default`, `[admin, reader]` | Ferry's service identity |
| `keystone.applicationCredentialName` | `ferry-manager` | the credential the manager finds or mints |
| `keystone.applicationCredentialId` | empty | set to use one you created; minting is skipped |
| `keystone.catalog.*` | register, `backup` | the catalog entry the manager keeps asserted |
| `kms.driver` | `none` | `none`, `barbican` or `vault`. Never `file` |
| `ceph.enabled` | true | read RBD from the cluster |
| `ceph.conf.configMap` / `secret` / `key` | `ceph-etc`, none, `ceph.conf` | the platform's `ceph.conf`, from a ConfigMap or a Secret |
| `ceph.users.*` | `ferry-manager`, `ferry-mover`, `ferry-restore` | the three cephx identities |
| `ceph.pools.*` | `volumes`, `vms`, `images` | Cinder's, Nova's and Glance's pools; `ferry` for Ferry's own |
| `ceph.removeNovaOrphans` | false | delete a `vms` disk Ferry's anchor kept after Nova deleted its instance; off, every such disk stays in the pool (see below) |
| `mover.drainTimeoutSeconds` | 90 | how long a stopping mover lets its jobs finish before it hands the rest back to resume on another mover; the pod's `terminationGracePeriodSeconds` is this plus 60. Set it here, not through `mover.extraConfig`, which the grace period does not follow |
| `compute.enabled` | true | the manager uses compute agents |
| `compute.daemonset` | true | the chart runs them; false where they run outside Kubernetes |
| `compute.port` / `adminPort` | 9779, 9780 | the agent's listener and its admin port, on loopback unless `metrics.enabled` |
| `compute.localDiskBackup` | false | back up and restore Nova local disks through the agent |
| `compute.novaInstancesDir` | `/var/lib/nova/instances` | where Nova keeps local disks on the host |
| `compute.qemu.uid` / `gid` | 42424 | who QEMU runs as, which must own the scratch |
| `compute.edpm.*` | off | run the agents on RHOSO EDPM nodes: `nodeSets`, `deploy`, `libvirtSocket`, `qemu` |
| `compute.libvirt.*` | `/run/libvirt`, `libvirt-sock` | where libvirt's socket is on the host |
| `pki.provider` | `cert-manager` | or `existing` |
| `api.ingress.*` / `api.route.*` | off | expose the API; the catalog's public URL follows |
| `metrics.enabled` | false | the managers' and agents' admin listeners (`/metrics`) on the pod's and the node's address instead of loopback |
| `metrics.serviceMonitor.enabled` / `labels` | off, `{}` | ServiceMonitors for the managers and movers, a PodMonitor for the agents |
| `metrics.prometheusRule.enabled` / `labels` | off, `{}` | Ferry's alert rules as a PrometheusRule |
| `dr.enabled` | false | let the managers release, recover, test, resync and retire protection groups; off, those requests answer `dr_not_enabled` |
| `dr.runDeadline` | `12h` | how long a recover may run before it stops; a test also stops at its bubble policy's maximum duration |
| `dr.probe.image` / `dr.probe.flavor` | empty | the Glance image and flavor a recover boots on each tier's network to run health checks, and a bubble's jump host falls back to; the image is `ferry-probe-<version>.qcow2`, attached to each Ferry release (upload it with `openstack image create ferry-probe --disk-format qcow2 --container-format bare --property hw_serial_port_count=1 --public --file ferry-probe-<version>.qcow2`) |
| `dr.probe.direct` | false | run a recover's health checks from the managers, which must then reach tenant networks |
| `ceph.mirror.driveSchedule` | false | let the managers take the rbd-mirror snapshots that replicate hot groups, instead of only watching the mirror's own schedule |
| `ceph.mirror.enableImages` | false | let the managers enable snapshot mirroring on a hot group's images where the workload runs |
| `console.productName` / `primaryColor` / `primaryColorDark` | empty | the console's name and brand colour |
| `console.filesConfigMap` + `logo` / `logoDark` / `favicon` / `stylesheet` | empty | the console's images and CSS, by file name in that ConfigMap |
| `extraObjects` | `[]` | anything else to render, templated: an HTTPRoute, a `KeystoneService` |
