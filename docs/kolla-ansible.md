# Installing Ferry on a kolla-ansible cloud

For someone (or something) standing in front of an OpenStack deployment they
did not build, with `kolla-ansible` on the deploy host and no prior knowledge of
Ferry.

Read [Decide what this cloud can protect](#0-decide-what-this-cloud-can-protect) before typing anything. Which
storage the cloud uses decides the inventory, the flags, and which hosts need
kernel modules, and it is the one thing that cannot be corrected afterwards
without redoing the install.

---

## 0. Decide what this cloud can protect

Ferry reads a disk one of three ways. A cloud usually needs more than one,
because most clouds have more than one kind of storage.

| Path | Reads | Needs | Cost |
|---|---|---|---|
| **Cluster** | Ceph RBD | a cephx key | Cheapest. Real incrementals via `diff-iterate`. |
| **Hypervisor** | anything a running guest has open: SAN LUN, LVM, NFS/qcow2, local file | `ferry-compute` on every hypervisor, and `ferry_local_disk_backup` | Reads cross a compute node. Incremental only where the disk is qcow2. |
| **Fabric** | anything Cinder can mask to a host: **detached volumes**, and volumes attached to a guest that is not running | a mover zoned into the storage network; volume quota in Ferry's own project | A LUN mapping and an initiator session per read. Always a full read. A stopped guest's volume additionally costs a temporary snapshot and copy. |

Answer these three, in order:

1. **Is any Cinder backend not Ceph?** `openstack volume type list`, then check
   each type's `volume_backend_name`. If yes, you need the hypervisor path,
   the fabric path, or both.
2. **Does the cloud have volumes no running guest has open?** Detached ones, and
   ones attached to an instance that is shut off, suspended or shelved. Neither
   has a QEMU to read through, so only the fabric path reaches them, the second
   kind through a temporary copy, because Cinder will not attach a volume it
   still calls in-use.
3. **Are any Nova instances booted from local disk?** `images_type` in
   `nova.conf`: anything other than `rbd`. Those roots are readable only
   through the hypervisor path.

A cloud that is Ceph end to end needs none of the hypervisor, storage-fabric or
bucket sections below. Do the minimum
install, verify it, and stop.

---

## 1. Prerequisites

- `kolla-ansible` 2025.1 or newer on the deploy host, and a cloud that already
  deploys cleanly. Ferry does not fix a broken cloud.
- **Ferry's role linked into kolla-ansible.** Ferry is an out-of-tree service:
  `kolla-ansible/install.sh` symlinks its role and its `ferry.yml` playbook into
  the kolla-ansible installation, and every `kolla-ansible` command for Ferry
  passes `-p <kolla-ansible>/ansible/ferry.yml`. The
  [kolla-ansible README](../kolla-ansible/README.md) has the commands.
- **A key manager (Barbican or Vault) if you want encrypted backups.** Not
  required; a cloud with neither can still back up, restore and replicate. See
  "Encryption, and whether you need a key manager" below for the choice and what it costs.
- **Object storage for the repository**: an S3 endpoint (RGW, MinIO, anything
  S3-compatible) with a bucket Ferry may write to. Backups go here. Without one
  there is nowhere for a backup to land.
- If the cloud uses Ceph: `ceph.conf` and the ability to create cephx users.

---

## 2. Inventory

Ferry adds four groups. Put them in your inventory, or in a second file passed
with another `-i`:

```ini
[ferry-manager:children]
control

[ferry-mover:children]
control

[ferry-compute:children]
compute

# Empty unless you need the fabric path; see "Reading through the storage fabric" below.
[ferry-mover-fabric]
```

What each is:

- **ferry-manager**: the control plane. Holds the database credential and the
  KEK. Put it where other API services live.
- **ferry-mover**: the data plane. Reads storage, writes the repository. Wants
  bandwidth and its own blast radius: a mover OOMing on a large volume must not
  take the API down with it.
- **ferry-compute**: the quiesce agent, and optionally the local-disk read
  path. Must be on **every hypervisor** whose guests should get
  filesystem-consistent backups; it talks to that node's libvirt socket and
  nothing else. Leaving a hypervisor out is supported: its guests get
  crash-consistent backups, reported as such.
- **ferry-mover-fabric**: movers zoned into the storage network. Empty by
  default and **stays empty on a Ceph-only cloud**. See "Reading through the
  storage fabric" below before filling it in;
  it has host prerequisites the others do not.

---

## 3. Encryption, and whether you need a key manager

You have three options and they are all supported. The one thing that is *not*
supported is a KEK on local disk.

| `ferry_kms_driver` | What you get |
|---|---|
| `barbican` / `vault` | Encrypted backups. A workload can have its own key, and then its own dedup domain, isolated from every other workload, which is what having its own key means. |
| `none` | No encryption. Every unencrypted workload shares one repository-wide plaintext domain, so they dedup against each other, which is where the space savings are. A plan that asks for encryption is refused **when it is created**, not quietly written in the clear. |
| `file` | **Refused.** See below. |

So: no, a key manager is not required. Pick `none` deliberately if the cloud has
no Barbican and no Vault, and you accept that what lands in object storage is
readable by anyone who can read the bucket.

**How much each is exercised.** Barbican and Vault have both been run end to end
against a real service: register a repository, back up, restore, and compare the
restored volume byte for byte. The Vault run also rotated the transit key to a
new version and re-verified a point written under the old one, which is the
property that makes rotation safe: Vault decrypts under the version that
wrapped it.
`none` has been run end to end as well, on a production cloud with no key
manager: register a repository, back up, restore, compare byte for byte, and a
filesystem-consistent instance backup through the quiesce agent. It needs
0.4.21 or later. Before that a repository could not be registered under it,
because its credential is sealed by envelope and `none` refuses to mint the
key. Since 0.4.21 that credential is stored as written, in a frame that opens
only under `none`.

What is *not* a real option is `file`, and the precheck fails on it. A data
encryption key is only ever stored wrapped under the KEK, so the KEK is the
whole repository, and a key on local disk has no replication, no backup, no
audit trail, and dies with the node. Losing it makes every point permanently
unreadable. That is a trap rather than a trade-off, which is why it is refused
rather than warned about.

### With a key manager

Barbican, which most clouds already have:

```yaml
ferry_kms_driver: "barbican"
ferry_kms_barbican_secret_id: "<uuid of a 32-byte secret you create>"
```

The role deliberately does **not** mint the key. Provisioning the thing that
decrypts every backup a tenant owns should leave a trail, not happen as a side
effect of a deploy, and a KEK reminted because a variable was empty is a
repository nobody can read. `kolla-ansible/tools/create-kek.sh` mints one and
prints the id to put here.

Vault instead. **Ferry does no AppRole login**: `role_id`/`secret_id` on their
own are not a runnable configuration, and the manager refuses to start on one.
Give it a token, or better a file a Vault Agent keeps a fresh token in: the
agent authenticates and renews, and Ferry re-reads the file rather than holding
a credential that expires under it.

```yaml
ferry_kms_driver: "vault"
ferry_kms_vault_address: "https://vault.internal:8200"
ferry_kms_vault_mount: "transit"
ferry_kms_vault_key_name: "ferry"
ferry_kms_vault_token_file: "/var/run/vault/ferry-token"   # a Vault Agent sink
# ferry_kms_vault_token: "<token>"                          # or a token directly
```

Ferry needs exactly three transit operations on one key, so the policy is small:

```hcl
path "transit/encrypt/ferry" { capabilities = ["update"] }
path "transit/decrypt/ferry" { capabilities = ["update"] }
path "transit/rewrap/ferry"  { capabilities = ["update"] }
```

Set the engine and key up first:

```bash
vault secrets enable transit
vault write -f transit/keys/ferry
```

Ferry does not move existing keys forward on its own: after `vault write -f
transit/keys/ferry/rotate`, new keys are wrapped under the new version and every
key already stored keeps decrypting under the version that wrapped it. Do not
raise the transit key's `min_decryption_version` while a stored key still uses
an older version. `rewrap` is granted for the key-rotation path, which
re-encrypts a key under the newest version **inside Vault** without it ever
leaving.

The token file is re-read whenever it changes, and once more when Vault refuses
a token, so a Vault Agent renewing it needs no manager restart.

### Without one

```yaml
ferry_kms_driver: "none"
```

Nothing else to configure. Backups, restores and replication all work; plans are
created with `--encryption none`, and asking for encryption on this cloud fails
at plan creation with that as the reason.

A cloud without a key manager also cannot hold a key imported from another
cloud, so as a disaster recovery region it recovers only groups whose members
the home region backs up under plans with `--encryption none`. Their group
document is written unencrypted and read here without a key. A group whose
members are in an encrypted dedup domain (including `default`, where a plan
that names no encryption lands on a cloud with a key manager) is named as such
by `ferry dr groups`, refused by `ferry dr register` with `key_manager_missing`,
and its readiness carries `key_manager_missing` on each such member.

---

## 4. The minimum install

In `globals.yml`:

```yaml
enable_ferry: "yes"

# The role pins the release it ships with. Set this only to pin another one, and
# never to `latest`: it moves on every commit, so two hosts deployed an hour
# apart would run different code.
#ferry_tag: "<release>"

ferry_external_fqdn: "ferry.example.net"

# true when the cloud has Ceph and Ferry should read RBD directly.
ferry_ceph_enabled: true
```

If `ferry_ceph_enabled` is true, put `ceph.conf` where the role expects it,
`{{ node_custom_config }}/ferry/ceph/ceph.conf`, and create the three cephx
users the role names: `ferry-manager`, `ferry-mover`, `ferry-restore`. Scope
them read-only on production pools and read-write only on Ferry's own. The
manager will not dispatch a job for a pool a mover has not advertised, so drift
between the key and the configuration is a refused job rather than a permission
error discovered mid-backup.

**Deleting an instance whose disk Ferry protects leaks that disk unless you set
`ferry_ceph_remove_nova_orphans: true`.** Ferry keeps an anchor snapshot on every
`vms/<uuid>_disk` it backs up. Nova's delete removes only its own resize snapshot,
gives up after a minute and reports the instance deleted, so the disk stays in the
pool at full size and nothing in OpenStack names it. Ferry finds it within one
reconcile pass, drops its anchor and counts it in `leaked_disk_bytes` on every
repository (`ferry_manager_leaked_disk_bytes` in Prometheus). It deletes the disk
only with the option on, and then only when Nova answers 404 for the instance and
the disk has no snapshot or clone of anyone else's. The default is off because
deleting a tenant's disk on the strength of an instance listing is your call. Ferry
drops the anchor once it stops protecting the disk (when the instance leaves its
plan, or when a deleted plan's soft-delete grace ends), so the exposure is a disk deleted
while protected or inside that grace. In `globals.yml`, then `kolla-ansible deploy` (the
managers restart with it):

```yaml
ferry_ceph_remove_nova_orphans: true
```

On a cloud that already has leaked disks, turning it on is enough: the next
reconcile pass over the Nova pool (within ten minutes with the default five-minute
loop and two pools) removes every recorded one, including those whose anchor an
earlier pass already dropped, and logs one line per disk:
`removed a disk Ferry's anchor leaked`, or `left a disk Ferry's anchor leaked in
place:` and the reason. To keep the option off, remove them yourself: list the
disks Ferry recorded from its database, confirm Nova answers "No server with a name
or ID" for each instance, and remove the disk. The next pass sees it gone and stops
counting it.

```bash
mysql ferry -e "SELECT image, size_bytes, detected_at FROM orphans
                WHERE kind = 'nova_orphan' AND resolved = 0"
openstack server show <uuid>
rbd rm vms/<uuid>_disk
```

**If you keep an instant tier, Cinder needs to read it too.** An instant restore
is an `rbd clone` out of the tier pool that Cinder adopts, and until Ferry's
background flatten finishes, every read of that volume follows the clone back
into the tier: Cinder's own, when it adopts it, and Nova's, when it attaches
it. Kolla's Cinder and Nova attach RBD volumes as `client.{{ ceph_cinder_user }}`
(`cinder` by default), and that key has no caps on a pool Ferry created. Add
read-only caps on the tier pool to whatever it already has. `ceph auth caps`
replaces the whole list, so repeat the existing caps:

```bash
ceph auth get client.cinder                       # note the current osd caps
ceph auth caps client.cinder mon 'profile rbd' \
  osd '<the existing osd caps>, profile rbd-read-only pool=ferry-tier'
```

`kolla-ansible/tools/create-cephx.sh` checks this key and prints the command
with the existing caps filled in. Without it every instant restore fails with
`Cinder could not open the clone; its Ceph identity needs read access to tier
pool ferry-tier until the flatten finishes`.

**If you keep warm standbys, Cinder needs to read their pool too.** A standby
promote, and a warm group's recover and test, clone the standby's applied
snapshot out of the standby pool and hand the clone to Cinder exactly as an
instant restore does, so the same key needs the same read-only caps on that
pool: until a promote's or a recover's flatten finishes, and for the whole life
of a test's clones, which are never flattened. Add it the same way, repeating
the existing caps, with the standby pool's name in place of the tier's:

```bash
ceph auth caps client.cinder mon 'profile rbd' \
  osd '<the existing osd caps>, profile rbd-read-only pool=ferry-standby'
```

Without it every warm recover or test fails at materialise with `Cinder could
not open the clone; its Ceph identity needs read access to standby pool
ferry-standby`, and the clone is left in `error`.

Then:

```bash
kolla-ansible -i <inventory> prechecks   # do not skip this
kolla-ansible -i <inventory> pull        # separate action; deploy does not pull a moved tag
kolla-ansible -i <inventory> deploy
```

**`pull` is separate and it matters.** The container start only pulls when the
image is *missing*, so a tag that moved leaves the old binary running and
nothing says so.

**Logs.** Ferry writes to `/var/log/kolla/ferry/` on the `kolla_logs` volume,
where kolla's fluentd reads every service's logs from, and rotates the files
itself. fluentd needs to be told about the directory once:

```bash
cp kolla-ansible/etc/fluentd/input/20-ferry.conf  /etc/kolla/config/fluentd/input/
cp kolla-ansible/etc/fluentd/filter/20-ferry.conf /etc/kolla/config/fluentd/filter/
kolla-ansible -i <inventory> reconfigure --tags common
```

`docker logs ferry_manager` keeps working regardless.

---

## 5. Reading disks through the hypervisor

Turn this on when any instance has a root or a volume Ferry cannot read from the
cluster. It makes `ferry-compute` a data-plane component (it will stream the
contents of a guest's disk to a mover), which is a different security posture
from the quiesce-only agent, so it is off by default and you should choose it.

```yaml
ferry_local_disk_backup: true
```

Two things about the scratch, both of which will cost you an afternoon if you
change them carelessly:

- **QEMU opens the scratch, not Ferry.** They are different processes, different
  users, and under kolla different mount namespaces. The scratch must be under
  `/var/lib/libvirt`, which is the one path both name identically. A path only
  the agent can see fails at `blockdev-add` with *"No such file or directory"*,
  naming a file the agent has just successfully created. The precheck refuses
  anything outside it.
- **It grows with guest writes** for the life of a backup session. Do not share
  a filesystem with the instances: filling that one stops the guests, which is
  far worse than a failed backup. Set a ceiling:

```yaml
ferry_compute_max_scratch_bytes: 21474836480   # 20 GiB; 0 disables
```

Guests are quiesced through the qemu-guest-agent where there is one. A guest
without one, or one that is paused, gets a crash-consistent backup and the point
says so. Set `ferry_require_quiesce: true` only if you would rather a plan fail
than silently downgrade.

---

## 6. Reading through the storage fabric

Needed for volumes on storage that is not Ceph (**detached** ones, and ones
attached to a guest no hypervisor can serve), and to restore onto them. Only
fill in `[ferry-mover-fabric]` once these hold:

**A fabric mover must be the only iSCSI stack on its host.** Not tidiness.
`iscsiadm` and `iscsid` talk over a private IPC whose structures are not stable
across open-iscsi releases, and Ferry's image is not built from the same base as
the cloud's. Where another daemon holds the socket, the mover's `iscsiadm` talks
to *that* daemon and is misparsed: discovery of a portal succeeds and every login
to it fails with `iSCSI driver not found`, on a host whose module is loaded and
whose transport is present in sysfs. So a fabric mover is **not** a hypervisor,
**not** a cinder-volume host, and runs no `iscsid` of its own. The precheck
refuses each of those.

**The kernel modules must be on the host.** Ferry cannot load what the host does
not have. On Ubuntu, `nvme_tcp` and `nvmet_tcp` ship in
`linux-modules-extra-$(uname -r)`, which is not installed by default; on
RHEL-family hosts they are in the base kernel package. The precheck tells you
which is missing before the deploy tries.

```yaml
ferry_fabric_iscsi: true        # default
ferry_fabric_nvmeof: false      # NVMe-oF/TCP; needs nvme_tcp on the host
ferry_fabric_multipath: false   # DEVICE-MAPPER multipath only
```

`ferry_fabric_multipath` is dm-multipath, which needs a running `multipathd`.
NVMe-oF does its own multipath in the kernel (ANA) and this switch neither
enables nor disables it; the connector detects that separately.

These hosts run **privileged**, with the host's `/dev`, `/lib/modules`, and a
persistent initiator identity: on its first start each mover generates its own
iSCSI initiator name into `ferry_iscsi` and NVMe host NQN into `ferry_nvme`, and
never changes one that is already there. Where it shares kolla's iscsid it uses
the host's name from kolla's `iscsi_info` instead. The role arranges all of it;
that is why fabric
movers are a group rather than a flag. Being in the fabric is zoning and
initiator registration on a real array, per host, and not something a deploy
can conjure.

**Give Ferry's own project volume quota.** A volume attached to a guest that is
not running cannot be attached to a mover (Cinder calls it in-use for as long as
it is attached), so Ferry reads a temporary copy: a snapshot, and a volume built
from it. Cinder creates both under the credential that asked, so they land
in **Ferry's** project (`service` on a kolla deploy), never the tenant's. That
project therefore needs, per copy in flight, one volume, one snapshot, and the
source volume's own size in gigabytes. The stock service-project quota is
10 volumes / 10 snapshots / 1000 GB, which protects one large volume at a time:

```bash
openstack quota set --volumes 50 --snapshots 50 --gigabytes 20000 service
```

Nothing else in Ferry uses that quota, and the copies are destroyed at the end of
each backup, so the headroom is sized by concurrency rather than by how much is
protected.

---

## 7. Buckets and file-level recovery

**Object storage.** Ferry protects buckets through one RGW realm. On a cloud
whose RGW kolla already knows about (`enable_ceph_rgw: "yes"`), one flag is the
whole change:

```yaml
ferry_objects_enabled: true
```

The S3 endpoint is then derived from kolla's own RGW variables, as
`http://<kolla_internal_vip_address>:<ceph_rgw_port>`. Note that this is
deliberately *not* the catalog's `object-store` entry: a deployment registers the
Swift-compatible endpoint there (`…/swift/v1`), so resolving through the catalog
finds the Swift URL and then 404s every S3 request. S3 lives at the root of the
same listener. Set `ferry_objects_endpoint` yourself for a gateway kolla does not
know about.

The gateway credential is an RGW user with `system` set, which reads any bucket
in the realm. Minting one needs `radosgw-admin` and a Ceph admin keyring, which a
kolla control host does not have when Ceph is deployed separately, so it is a
tool, run once on a mon or wherever you ran `create-cephx.sh`:

```bash
kolla-ansible/tools/create-rgw-user.sh
```

It creates `ferry-gateway`, sets `system` on it if it is missing, and writes the
keys to `/etc/kolla/config/ferry/objects.yml` (mode 0600), which the role loads
on the deploy host. Re-running it reads an existing user's keys back rather than
rotating them, so it never invalidates a deployed manager. Copy that file to the
deploy host if you ran the tool elsewhere, or set `ferry_objects_access_key` and
`ferry_objects_secret_key` yourself.

Own the copy as the user who runs `kolla-ansible`. The role reads it with become
disabled. For the reason `appcred.yml` sets out (kolla's inventory carries
`ansible_become=true` in `[all:vars]`, and that beats a task's `become` for a
`delegate_to: localhost` task), a root-owned copy fails the deploy with
"Could not find or access".

Turning the flag on with no credential fails the deploy with those instructions,
rather than rendering a manager that answers every bucket operation with a 403.

Leave `ferry_objects_enabled` off and bucket plans are refused with that as the
reason, and the console hides the feature: the honest answer for a cloud with no
gateway.

**File-level recovery** lets a tenant browse a point and pull individual files
out of it:

```yaml
ferry_enable_flr: true
```

This swaps the mover for an image carrying libguestfs, and the role configures
the browse listener and its mTLS. The listener accepts an unwrapped
data-encryption key and returns tenant file contents, so it is never plaintext
and never reachable by anything but a manager.

---

## 8. Get the CLI

Everything from here uses `ferry`, and nothing installs it for you.

It is a **static binary**, built `CGO_ENABLED=0`, and it ships inside every
Ferry image. Copy it out of one onto whatever host you administer from:

```bash
id=$(docker create ghcr.io/osie/ferry-manager:0.2.1)
docker cp "$id:/usr/local/bin/ferry" /usr/local/bin/ferry
docker rm "$id"
```

Or, if you would rather not put it on the host, run it in place. This is also
the quickest way to check a fresh deploy from the control node itself:

```bash
docker exec -e OS_AUTH_URL -e OS_USERNAME -e OS_PASSWORD -e OS_PROJECT_NAME \
  -e OS_USER_DOMAIN_NAME -e OS_PROJECT_DOMAIN_NAME -e OS_IDENTITY_API_VERSION \
  ferry_manager ferry plan list
```

**Authentication is ordinary OpenStack.** `OS_*` environment variables and
`clouds.yaml` are both honoured, and `--os-cloud` selects an entry, so an
existing `admin-openrc.sh` or `clouds.yaml` already works:

```bash
source /etc/kolla/admin-openrc.sh
ferry plan list
```

**The endpoint is discovered from the catalog**, under service type `backup`,
which the role registers. You do not configure a URL. If the CLI cannot find
it, the endpoint is missing or your token is scoped to a project that cannot
see it. Check with:

```bash
openstack endpoint list --service backup
```

**Scope matters, and the operator commands need more than `admin`.** Plans,
points and restores belong to a project: a tenant sees their own, an operator
sees what their token is scoped to. But `repository`, `policy`, `legal-hold`,
`coverage` and `admin` need a **system-scoped** token. An admin role on a
project is not enough, deliberately, because an admin role on *some* project is
what every Horizon session carries. `policy list` is the one exception: a tenant
may read the policies their project may name, as sentences.

```bash
source /etc/kolla/admin-openrc-system.sh   # OS_SYSTEM_SCOPE=all
ferry repository list
```

A `403 policy denies repository:...` (or `policy:write`, `policy:assign`,
`repository:lock`, `legal_hold:write`, `coverage:read`) on an otherwise working
install is almost always this.

**Promoting a standby, and restoring into another project, need a role in that
project.** Both act in the target project through a Keystone trust the caller
grants, and Keystone delegates only roles the caller holds in that project
itself: an admin role on the system, or on some other project, does not count.
So an operator account that runs `ferry standby promote`, or restores a
tenant's backup (back into the tenant's project, or into another with `ferry
restore create --target-project`) needs a role, `member` is enough, in every
project it acts in. Without one the request is refused with `403 restore_trust_refused`,
*no role on the target project*, before anything is created. Grant it per
project, and use a password session: Keystone will not delegate from a token
issued by an application credential either.

```bash
openstack role add --user <operator> --user-domain Default \
  --project <target-project-id> member
```

---

## 9. Register a repository: a deploy alone backs up nothing

The role does not create one. Until a repository exists there is nowhere for a
backup to go, and every plan is unsatisfiable.

**Create the bucket first.** `register` records what you tell it and does not
check that the bucket exists; a wrong or missing one surfaces at the first
backup as `bucket ... does not exist`, long after the command that accepted it.

```bash
ferry repository register \
  --name primary --kind chunk --driver s3 \
  --site <your site name> \
  --endpoint https://s3.example.net --bucket ferry-backups \
  --region us-east-1 \
  --access-key <key> --secret-key <secret>
```

The site name defaults to the region, lowercased (`ferry_site_name`). Movers
claim work from their own site, so this has to match.

A repository another site can read too (a bucket the recovery region reaches,
or a replica the store keeps there) says so with `--reachable-from-site <site>`
(repeated, a name or an id; `ferry site list`) or `--replica-of <repository>`.
A policy that recovers at another site needs one. `ferry repository update`
replaces either, and `--clear-reachable-from-sites` and `--clear-replica-of`
remove them.

Registering the *other* region's repository here, at its own site, is how this
region reads what that one writes: its backups, its group documents and its
handover records. The manager lists such a repository with the keys you give, or
with none, before it saves it, and refuses one it cannot list with
`repository_unreadable`, naming the error. Give it an S3 key pair that can list
that bucket; a private RGW bucket answers an unsigned request as if it did not
exist. `--environment-credentials` skips the check when the manager's and the
movers' own environment supplies the key; `ferry repository list` then shows the
repository's credentials as `NONE, other site` and readiness warns
`repository_credentials_missing` until a key pair is sealed with `ferry
repository update`, which lists again with the new keys before saving them.

### The retention contract

A repository carries the whole of what "cannot be erased before it expires"
means. Nothing a plan or a policy says changes it, and no plan asks for it.

For immutable backups, create the bucket **with Object Lock enabled** and
register the contract with it:

```bash
ferry repository register \
  --name primary --kind chunk --driver s3 --site <your site name> \
  --endpoint https://s3.example.net --bucket ferry-backups \
  --access-key <key> --secret-key <secret> \
  --lock-mode compliance --lock-min-retention 30d --soft-delete-grace 14d
```

- **`--lock-min-retention` is a floor, not a retention.** Every object written here is
  held until `max(point expiry, written + min_retention)`. A point's expiry is
  decided when it commits, from the roles it won in the policy's ladder, so a
  daily point is locked for the daily's horizon and not for the yearly's.
- **A policy below the floor is refused, not clamped.** If a policy's shortest
  tier (`keep.all`) is under `min_retention`, creating it fails naming the floor.
  That is deliberate: clamping would silently sell a service level nobody wrote.
- **`--lock-mode governance` is registered as what it is.** The tenant sentence
  becomes *cannot be erased before expiry except by an operator with bypass*,
  which is weaker on purpose. Ferry never exercises the bypass.
- **There is a grace period for typos.** `changeable_until` defaults to three
  days after registration. Until then the mode, the floor, the lock granularity
  (`ferry repository update --lock-granularity`, seven days by default) and the
  block itself may be changed or removed freely. After it, `min_retention` only
  increases, the mode and the granularity are fixed, the block cannot be removed,
  and the repository cannot be unregistered while it still holds an object under
  lock. Every refusal names the date that passed. An operator who is certain sets
  `--lock-changeable-until` to the current time.
- **`--soft-delete-grace` is the only thing between a stolen tenant token and an
  empty project** on a repository with no lock. A point or plan a tenant deletes
  is hidden from them and kept for the grace, restorable by an operator. A grace
  of 0 means delete means delete.

Editing the contract later is `repository:lock`, which is a separate action from
`repository:register` so a deployment can put it behind a second role.

---

## 10. Write a policy: a plan cannot be created without one

**The operator writes policies; a tenant names one.** A plan carries no schedule,
no retention, no RPO and no immutability flag: every one of those is read off the
policy the plan names, and the policy is yours. A tenant who needs something no
policy offers asks for a policy, which is how a service works. The one thing a
plan states itself is its consistency (`ferry plan create --consistency`), because
it depends on the resource kind and a policy is written for every kind.

```bash
ferry policy create --name gold \
  --description "hourly, a month of dailies, a year of monthlies, seven yearlies" \
  --every 1h --at 02:00 --window-start 22:00 --window-end 06:00 \
  --jitter 30m --start-within 2h \
  --keep "all=48h,daily=30,weekly=12,monthly=12,yearly=7" \
  --anchors "week=sunday,month=first,year=january" \
  --backup-repository primary \
  --instant-repository ceph-tier --instant-within 168h \
  --copy-repository secondary --copy-keep "daily=7,monthly=12" \
  --recovery-site <recovery-site> --rung warm --transport repository \
  --rpo-target 1h --rto-target 30m \
  --verify-every 720h
```

What a tenant may do by hand is the vault's, not the policy's. Offer the
repository to tenants as a vault, with the terms of a manual backup kept there:

```bash
ferry vault create --repository primary --name Immutable \
  --description "Kept until its date. Nobody can delete it earlier." \
  --max-keep 90d --max-per-day 4
```

Without `--public` it reaches the projects of the policies that write to the
repository. On a repository with no lock, `--until-deleted` also offers "until I
delete it".

What each group does, and the one refusal worth knowing about in advance:

- **`--every` and `--at` replace a cron expression.** `--cron` exists for
  "weekdays only" and an operator who knows what they are doing; a tenant never
  sees either. `--window-start`/`--window-end` and `--jitter` are the load knobs,
  stated once for every plan under the policy, which is how 400 plans on one
  cadence stop being 400 chances to type `0 2 * * *` with no jitter. A window may
  cross midnight; one that opens and closes at the same minute is refused, since
  no backup could ever start in it.
- **`--start-within` is a deadline, not a queue.** A tick that has not dispatched
  by then is recorded as **missed** and does not run behind the next one, because
  a plan six hours behind must not send two backups at once into a production
  cluster. The misses are counted, per plan, in the coverage report.
- **The ladder keeps one point per anchored period, and the first one.** The first
  point that commits in a day wins the daily; if it also opens the week, the month
  and the year, it holds all four at once and lives as long as the longest. Roles
  are claimed at commit and the expiry is written once. A missed or failed tick
  passes the role to the next point that commits in the period.
- **`--keep-all` is a duration**, because a count names no date and a date is what
  the repository's lock needs.
- **The tiers name repositories you registered.** `--backup-repository` is
  required. `--instant-repository` is the RBD tier and `--instant-within` how long
  a point stays instantly restorable. `--copy-repository` is a second chunk
  repository every committed point is copied to, with its own ladder. Register a
  store that replicates on its own with `--replica-of` instead and write no copy
  tier. The copy's ladder has no anchors of its own: its weeks, months and years
  start where `--anchors` says, and an edit that leaves `--anchors` out keeps them.
- **`--recovery-site` takes a site name or an id.** `ferry site list` shows the
  registered sites with both. A name no site carries, or one two sites carry, is
  refused before anything is sent, and an id no site is registered under is
  refused by the manager. A repository the policy writes to must be readable at
  that site: local to it, registered with `ferry repository register|update
  --reachable-from-site <site>`, or with a replica there registered with
  `--replica-of <repository>`. Otherwise the policy is refused with `no
  repository this policy writes to is reachable from that site`.
- **`--rung` is a promise about capacity you provisioned; it builds nothing.** It
  sets the recovery sentence a tenant reads, and a `cold` or `warm` rung is
  frozen onto every point as the `dr.ferry.io/tier` label. `cold` is the copy
  tier or the store's own replication. `warm` promises a standby kept current at
  the recovery site, and you create that standby: either a recovery binding on
  the recovery cloud with a standby pool (`ferry recovery-binding create ...
  --standby-pool <pool>`), which registers the standby when its first point is
  adopted, or `POST /v1/standbys` (`ferry standby create --project <project-id>
  --resource <id> --pool <pool>`). Promoting one needs a role in the standby's
  project, which an operator account does not have by default. `hot` is a
  protection group's mirror relationship. An RPO target
  shorter than the cadence is refused on a `cold` or `warm` rung rather than
  measured and missed, because there is nothing newer to replay.
- **`--transport` follows the rung.** `hot` is carried by `rbd-mirror`, the two
  clusters' own snapshot mirroring; `cold` and `warm` by `repository`, the backups
  written where the recovery site can read them. Left out, it is the rung's own;
  any other pairing is refused. There is no Ferry-driven transport between
  regions. The policy names no bubble policy: the isolated network a test runs in
  is the recovery region's to choose.
- **A protection group names one of these policies**, and its recovery section is
  the group's whole promise: the group itself carries no RPO, RTO, DR site or
  transport. Every member must already be protected by a plan under the policy,
  or the group is refused naming the member. `ferry group create -f group.yaml
  --policy <policy-id>`; a group from before this rule names none, is promised
  nothing, and `ferry coverage show` lists it.
- **Release, recover, test, resync and retire stay off until you turn them on.**
  `ferry_dr_enabled` (default `false`) is the manager's `dr.enabled`. Off, every
  one of those requests answers `503 dr_not_enabled` and `GET /v1/capabilities`
  reports `dr: false`. A run's history, its steps and what it created stay
  readable under `/v1/protection-groups/{id}/runs` either way.
- **A recover runs its health checks from a probe host it boots** on each tier's
  network, and a test's jump host falls back to the same image. Each Ferry
  release attaches that image as `ferry-probe-<version>.qcow2`, with its
  `.sha256` beside it. Upload it in each region that recovers or tests, and set
  `ferry_dr_probe_image` to its name or id and `ferry_dr_probe_flavor` to a
  flavor with a 1 GiB root disk and 256 MiB of RAM or more:

  ```bash
  sha256sum -c ferry-probe-<version>.qcow2.sha256
  openstack image create ferry-probe --disk-format qcow2 --container-format bare \
    --property hw_serial_port_count=1 --public --file ferry-probe-<version>.qcow2
  ```

  `ferry_dr_probe_direct: true` runs the checks from the managers instead, which
  must then reach tenant networks.
- **Hot groups (rbd-mirror) need the mirror relationship to exist already.** The
  storage operator enables image-mode mirroring on the Cinder pool on both
  clusters, bootstraps an `rx-tx` peer and runs an rbd-mirror daemon on each.
  `ferry_ceph_mirror_drive_schedule` (default `false`) lets Ferry take the
  mirror snapshots instead of `rbd mirror snapshot schedule`;
  `ferry_ceph_mirror_enable_images` (default `false`) lets Ferry enable snapshot
  mirroring on a hot group's images where the workload runs. The manager's key
  needs `profile rbd` on every Cinder pool a hot group's volumes are in, including
  a pool added for a mirrored volume type, and that pool must be listed in
  `ferry_ceph_volume_pools` for `dr_reason` to see its mirroring. Testing a hot
  group clones a mirror snapshot by its id, which needs a Ceph 19 (Squid) client
  library in the manager image. The stock manager image carries 19.2; one built
  with an older library refuses a hot test `mirror_unavailable` and everything
  else runs. The mover images keep Debian's 18.2, because a mover never clones
  by id.
- **`--mandatory` means a tenant may not pause or delete a plan under this
  policy**, or delete its points. The operator path is a separate action,
  `plan:pause:mandatory`, so relaxing `plan:delete` in your own `policy.yaml`
  does not relax this.

### Assign it

A policy is `assigned` by default, which means it is usable only where you offer
it. `--visibility public` makes it usable by every project in the cloud.

```bash
ferry policy assign gold --project <project-id>     # or --domain <domain-id>
ferry policy assign gold --domain acme --remove
ferry policy list                                   # as an operator: all of them
```

A tenant's `ferry policy list` shows only the policies their project may name,
rendered as a sentence, with no repository and no site id in it. The console
preselects the policy when exactly one is usable.

### Change it, deprecate it, retire it

- **Edits apply forward.** Every point already written keeps its expiry and its
  lock; the edit changes what the next point is written under. This is the one
  behaviour that never surprises.
- **`--apply-to-existing` re-derives expiry over points already written**, after
  a what-if that says what it would free. Lengthening is an ordinary edit;
  **shortening is its own action**, `policy:shorten`, so a deployment can bind it
  to a second role. On a locked repository it re-derives the dates and then finds
  every point still held, and the what-if says so before the edit does.
- **`ferry policy deprecate gold`** stops new plans naming it; existing plans run
  on, and the coverage report lists them.
- **`ferry policy retire gold --successor platinum`** moves every plan to the
  successor in one transaction. The successor must be another active policy, not
  weaker, and offered to every project whose plans move. Assign it there first,
  or the refusal lists the projects it is missing. Points keep their dates
  through the move. A retire with no plans under it needs no successor.

### Legal hold

The one retention control that is neither the policy's nor the repository's:
expiry skips a held point whatever its own expiry says, and the hold is lifted by
the same hand that placed it.

```bash
ferry legal-hold place <project-id> --scope project --reason "case 2026-114"
ferry legal-hold place <point-id> --scope point --reason "case 2026-114"
ferry legal-hold list
ferry legal-hold lift <hold-id>
```

It exists so that "keep everything about this project until the lawyers say
otherwise" is one call rather than a policy edit somebody has to remember to undo.

### The coverage report

```bash
ferry coverage show
```

Per project and per domain: resources under no plan at all, plans paused and for
how long, plans on a deprecated policy, plans reported **unsatisfied**, ticks
missed inside `retention.missed_tick_window`, points the copy tier has not reached
yet, points sitting outside any locked repository, and policies below this
cloud's own minimum. That last one is yours to state, in the manager's
configuration, and it is reported rather than enforced:

```yaml
retention:
  coverage_minimum_keep_all: 48h
  coverage_requires_lock: true
  missed_tick_window: 168h
```

In the kolla role those are `ferry_coverage_minimum_keep_all`,
`ferry_coverage_requires_lock` and `ferry_missed_tick_window`.

The same figures are published as `ferry_coverage_*` gauges, labelled by project,
by a loop of their own on a five-minute cadence:
`ferry_coverage_interval` and `ferry_coverage_metrics_enabled` in the role, or
`loops.coverage` in the manager's configuration. It is a separate loop on purpose:
turning off "how full is the repository" must not also turn off "what is not
protected". `loops.policy_verify` is separate from `loops.verify` for the same
reason: the manager's rolling verification and the cadence an operator sold in a
policy are two different promises.

### The zone a schedule is read in

A policy's `at`, its window and its ladder anchors are wall-clock, so they need a
zone. The manager uses `retention.timezone`, and with none set it falls back to
the host's own, which inside a container is UTC, so `at: "02:00"` fires at 02:00
UTC however the hypervisor is configured. State it explicitly if the operator
writing policies thinks in local time:

```yaml
retention:
  timezone: Europe/Bucharest
```

`ferry_retention_timezone` in the kolla role. The zone is read from the container's
tzdata, which the image carries. A daily schedule keeps its wall-clock time across
a DST change rather than drifting by the offset, and the hour that does not exist
on a spring-forward day resolves forward rather than being skipped.

Every write in this section (the policy, the assignment, the contract, a
shorten, a hold, a mandatory override) is in the audit log with who and what.

---

## 11. Verify

Do not declare success on a green deploy. Prove a round trip:

```bash
ferry resource list --kind volume        # is anything protectable, and what is refused?
ferry policy list                        # which policy may this project name?
ferry plan create --name smoke --kind volume --resource <volume-id> --policy gold
ferry point create --plan <plan-id> --keep-until 30d
ferry job watch <job-id>
```

`plan create` takes no schedule and no retention: both come from `--policy`. A
request that names one is refused naming the policy field that holds it now, so a
script written against the old shape fails loudly rather than creating a plan that
does something else. `point create` is the on-demand path, and `--keep-until` is
a date within the terms of the vault the plan's backups are kept in.

Then restore it and compare. A backup that has never been restored is a
hypothesis:

```bash
ferry restore create --point <point-id> --mode volume --name smoke-restored --wait
```

`ferry resource list` is the most useful command here. `PROTECTABLE` and
`REFUSAL` tell you what this cloud can and cannot do **before** a plan exists,
and a refusal names what to change.

Your own name, logo and colour on the console are `ferry_console_*` in
`globals.yml`: see [console-branding.md](console-branding.md#kolla-ansible).

### Monitoring with kolla's Prometheus

Every Ferry container serves `/metrics` on an admin port: 9775 on a manager,
9782 on a mover, 9780 on a compute agent. By default those listen on loopback,
where kolla's Prometheus cannot reach them. Two `globals.yml` settings change
that:

```yaml
ferry_admin_bind_internal: true      # bind the admin ports on api_interface
ferry_prometheus_integration: true   # install Ferry's scrape config and alert rules
```

The first binds the admin listeners on the internal API network instead of
loopback. They are never put behind haproxy, and they also serve `/loglevel`,
so keep that network to the control plane. The second makes `kolla-ansible deploy`
write `prometheus.yml.d/50-ferry.yml` (jobs `ferry_manager`, `ferry_mover`,
`ferry_compute` over the three Ferry groups) and `ferry.rules` into
`{{ node_custom_config }}/prometheus/`. Kolla picks both up on the next
Prometheus reconfigure:

```bash
kolla-ansible deploy -i <inventory> -i /etc/kolla/ferry-groups -p <kolla-ansible>/ansible/ferry.yml
kolla-ansible reconfigure -i <inventory> -t prometheus
```

The rules page on a pause that outlasts 30 minutes (`ferry_manager_dispatch_pause_cause`
names the watch holding it and why), a dead-lettered job, a plan with resources
past their cadence (`ferry_manager_plan_resources_overdue`), a repository whose
sweep is degraded or stale, a guest left frozen, and movers that cannot reach a
repository (`ferry_mover_repository_unreachable_total`, by cause). `/metrics`
carries per-project quota figures, so it is an operator surface only.

#### When `FerryReferencedObjectMissing` fires

A backup extending locks before its commit, or a relock, asked the repository to extend the lock
on an object a point references, and the repository answered that the object does not exist. Only
that path pages. A chunk that disappears while a backup is about to reuse it is uploaded again
under the same content address, which makes every point sharing it whole. It is counted as
`reuploaded` and recorded as an `object_reuploaded` event.

What Ferry has already done:

- The job failed with `referenced_object_missing` and committed nothing.
- For a backup, the previous point of the same plan and resource is now `broken` (a `base_broken`
  event), so the next backup reads the whole disk.
- Coverage, RPO and `ferry_manager_plan_resources_overdue` count only `committed` and `verified`
  points, so the broken point stops counting at once.

What it has not done: older points that share the object are still `verified`. Ferry keeps no index
from an object to the points that use it, and the rolling verification pass reaches them only on
its cadence.

1. **Find the object.** `ferry job list --state failed --kind backup` (or `--kind relock`) shows the
   job with error code `referenced_object_missing`. `ferry job show <job-id> -o json` lists its
   `object_missing` event, which names the repository, the object key and the point. The mover log
   carries the same line.
2. **Find the damaged points.** Chunks are shared mostly along one resource's chain, so start there.
   `ferry point list --plan <plan> --resource <resource>`, then `ferry point verify <point-id>` on
   each, newest first. A verify re-reads every chunk the point references and marks it `broken` if
   one is missing. If those turn up damage, widen to the other plans that write to the same
   repository.
3. **Find what deleted it.** Ferry's sweep deletes only objects no live point references, and a
   locked repository refuses deletes before the lock date. Compare the time with the sweeps on that
   repository (`ferry job list --kind gc`). Then check the bucket's lifecycle rules, its access or
   audit log, and any credentials other than Ferry's that can delete in the bucket.
4. **Protection comes back on its own.** The next scheduled backup of the resource reads in full
   and commits a whole point. If the RPO cannot wait, run it now with
   `ferry point create --plan <plan> --resource <resource>`.

---

## 12. When it does not work

Every row below is a real failure with a misleading first impression.

| Symptom | What it actually is |
|---|---|
| `non_rbd_backend` on a volume you expect to work | The read path it needs is off. Attached → `ferry_local_disk_backup`. Detached → a host in `[ferry-mover-fabric]`. |
| `blockdev-add: No such file or directory`, naming a file that exists | The scratch is not under `/var/lib/libvirt`, so QEMU cannot see it. |
| iSCSI login fails `iSCSI driver not found`, module loaded, discovery works | Another `iscsid` on the host, version-skewed against the mover's `iscsiadm`. |
| `Volume ... status must be available ... but the current status is reserved` | A previous attach failed after Cinder reserved it. Ferry reclaims what it can prove nobody owns; if it refuses, it names what holds it and what to run. |
| `volume ... is error_restoring` | A previous restore did not finish, so the volume holds neither the old contents nor the new. Restore into a fresh volume, or release it deliberately. |
| `backup needs a source with images`, though `PROTECTABLE` said yes | The plan's resources resolved to nothing this mover can read. `ferry plan refusals <id>`, and check the read path that resource needs is enabled. |
| Instance refused `instance_mid_move` | A resize awaiting confirmation has a disk on two hypervisors and Nova names the one a revert would discard. Confirm or revert it. |
| Instance refused `instance_rescued` | The root Nova reports is the rescue image. Unrescue it. |
| A policy reported `unsatisfied`, and no plan under it ticks | Its repository is gone, unhealthy, of the wrong kind, or cannot hold the ladder its floor requires. Nothing is moved anywhere else, on purpose. `ferry policy list` names the reason; fix the repository or edit the policy. |
| `422` on a `plan create` that used to work | `schedule`, `jitter`, `window_*`, `intent` and `placement_pin` are the policy's now. The refusal names the policy field that holds each one. |
| A tenant cannot pause or delete their own plan | The policy is `mandatory`. `plan:pause:mandatory` is the operator path, and it is a separate action from `plan:update`. |
| `403 restore_trust_refused`, *no role on the target project*, on a standby promote or a restore of a tenant's backup, from an operator who can do everything else | Keystone delegates only roles the caller holds in the target project, and a system-scoped admin holds none there. Give the operator account a role (`member`) in that project. |
| `422 spec.recovery.site_id: no repository this policy writes to is reachable from that site` | No repository the policy writes to is local to the recovery site or registered as readable there. `ferry repository update <repository> --reachable-from-site <site>`, or register the replica that is there with `--replica-of`. |
| A deleted point is still listed to an operator | Soft delete. It is hidden from the tenant and kept for the repository's `soft_delete_grace`, restorable until then. |
| A moved tag did not take effect | `deploy` does not pull. Run `pull` first. |
| Every instant restore fails `Cinder could not open the clone`, and `cinder-volume` logs `rbd.PermissionError … at snapshot None` in `manage_existing_get_size` | Cinder's key cannot read the tier pool the clone's parent lives in. Grant `client.cinder` `profile rbd-read-only pool=<tier pool>` alongside its existing caps. |
| A warm recover, a warm test or a standby promote fails `Cinder could not open the clone` naming the standby pool, and the cloned volume is in `error` | The same, for the standby pool: grant `client.cinder` `profile rbd-read-only pool=<standby pool>` alongside its existing caps. |
| An instant restore fails `quota_exceeded` naming Ferry's own service project | Cinder adopts every clone in Ferry's project before handing it over, so that project's volume quota is what counts. Raise it. If Cinder counts volumes the project does not have, correct `quota_usages` for it instead; Ferry no longer leaves that drift behind. |

---

## 13. Changing the key store on a cloud that already has backups

Short answer: don't, unless you mean to abandon what is there.

A data key is stored wrapped, and the wrapped form **names the KMS that produced
it**. Point a cloud at a different key store and every existing repository fails
to unseal with exactly that (*"wrapped key was produced by barbican, this KMS
is vault"*), which is honest, precise, and still means those points are
unreadable until you point it back. Nothing is corrupted: switch back and they
return.

Two consequences worth knowing before you try it:

- **Dispatch stops cloud-wide.** `limits.pause_when_repository_down` defaults to
  true, so once no chunk repository at the site can be opened, the manager
  pauses rather than dead-lettering every job in the window. That is the right
  behaviour and it also means a *new* repository under the new KMS gets no work
  until the old ones are dealt with.
- **A new repository is a separate registration.** The KMS seals repository
  credentials at `register` time, so a repository registered under Barbican
  stays a Barbican repository. Migrating means registering a new one under the
  new key store and letting retention age the old one out. There is no
  in-place re-key.

---

## 14. Upgrading

Check out the new release's role (its `ferry_tag` moves with it), re-run
`install.sh` if kolla-ansible itself was upgraded, then:

```bash
FERRY=<kolla-ansible>/ansible/ferry.yml
kolla-ansible pull    -i <inventory> -i /etc/kolla/ferry-groups -p $FERRY
kolla-ansible upgrade -i <inventory> -i /etc/kolla/ferry-groups -p $FERRY
```

`upgrade` rewrites `ferry.rules` in the node custom config, but Prometheus reads
it only on `kolla-ansible reconfigure -t prometheus`. Run that after any release
whose notes change the alert rules.

**Managers roll forward first, and do not roll back past a migration.** The
manager applies migrations at startup and refuses a binary/schema mismatch
loudly, so once a manager of the new version has started, an older one will not.
Read the release notes: they name the migrations a release adds, separately from
its commits, for exactly this reason.

**A mover being restarted hands its jobs back.** A mover told to stop lets its
jobs finish for `ferry_mover_drain_timeout_seconds` (90), then hands whatever is
still running back to resume on another mover from what it already wrote,
without spending an attempt. The role stops the mover container with
`graceful_timeout` set to `ferry_mover_stop_timeout` (the drain plus 60
seconds, for the cut jobs to unwind and report) on a deploy, an upgrade and
`kolla-ansible stop` alike, and gives `client_timeout`, how long kolla waits
for the stop before it removes the container, half a minute more. Kolla runs
each container under a systemd unit whose stop timeout is systemd's default of
90 seconds, which would cut the drain short, so the role also gives the mover's
unit a drop-in, `kolla-ferry_mover-container.service.d/ferry-stop.conf`, that
raises it to the same half minute past the stop timeout. A Docker daemon restart
is the one stop the role cannot stretch: without `live-restore`, Docker stops
every container with its own short timeout, so stop the mover's unit first
(`systemctl stop kolla-ferry_mover-container`), which drains it, before
restarting Docker on that host. The first
deploy of a release that carries these settings still stops the old mover with
kolla's own timeout, because kolla rewrites a container's unit only when it
recreates or restarts it. Raise the drain if your backups often have a few
minutes left when you deploy, and leave the stop timeout derived from it. A
mover that dies without draining (killed, crashed, out of memory) is reported
on each of its jobs once the lease runs out: `mover … stopped reporting during
attempt N of M`.

### Upgrading to 0.5.0: every existing plan needs a policy

0.5.0 moves the schedule, the retention ladder and the consistency level from the
plan onto an operator's protection policy, and `0041` drops those
columns. A plan that existed before the upgrade therefore comes out of it naming
**no policy**: it is listed, it keeps its resources and its key, and it takes no
backups at all until an operator puts it on one. Nothing guesses a policy for it,
because the guess would be a service level nobody agreed to, and a plan silently
attached to the wrong ladder is worse than one visibly stopped.

So plan the upgrade as three steps, and do the first one before the upgrade if you
can: write the policies the cloud already runs, then move the plans.

```bash
ferry policy create --name gold --backup-repository <id> \
  --every 24h --at 03:00 --keep all=48h,daily=30,monthly=12
ferry policy assign gold --project <project-id>

ferry coverage show                       # plans_without_policy names every one
ferry plan update <plan-id> --policy gold # one plan onto it
```

`ferry coverage show` is the checklist: `plans_without_policy` is the count that has to
reach zero, and it lists the plans with the project each belongs to. The same
figure is on `GET /v1/coverage` and in the console's Coverage page, and the plans
appear there under "not running" with that reason in words. Until it is zero, some
of this cloud is not being backed up, and it will not alarm on its own, because
a plan with no policy has no schedule to miss.
