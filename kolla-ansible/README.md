# Ferry on kolla-ansible

An out-of-tree kolla-ansible service. `kolla-ansible deploy -p .../ferry.yml`
puts Ferry's three containers on the cloud, registers it in that cloud's own
Keystone catalog as service type `backup`, gives it a database and a
service user, and puts its API behind the same haproxy every other service is
behind.

It is not a patch to kolla-ansible. Nothing upstream changes; the role and
playbook are symlinked into an existing installation by `install.sh`, and
`--uninstall` takes them out again.

## Supported kolla-ansible releases

**2025.1 Epoxy and newer.** The playbook refuses anything older, by name, in its
first play.

2024.2 Dalmatian is refused rather than shimmed, and the reason is worth
knowing: `kolla_container_facts` there accepts the `action` parameter but still
returns a *flat* result, so Ferry's `container_facts.containers[...]` lookups are
all undefined, every port precheck runs, and a re-run against a live Ferry
reports a port already in use. That is a wrong answer dressed as an environment
problem, which is worse to debug than the outright failure 2024.1 gives (where
the module has no `action` parameter at all).

One thing is expressed differently below 2026.1 Gazpacho, with no effect on the
result. `kolla_toolbox` stringifies its `module_args` before that release, and a
list of dicts does not survive it, so `appcred.yml` omits the explicit `roles:`
list there. Measured on a live cloud, that changes nothing: a credential created
with the list and one created without it come back identical, both carrying
every role the `ferry` service user holds. Keystone does not narrow the
credential to the requested list, so the list states intent rather than
constraining anything.

**The KEK needs an ACL, and `create-kek.sh` now sets one.** Barbican secrets are
project-scoped, and Ferry does not read the KEK as admin: `ferry-manager`
authenticates with its own application credential, as the `ferry` user in the
`service` project. A KEK created under admin and left alone makes the very first
repository registration fail with `403 Forbidden - Secret payload retrieval
attempt not allowed`, surfaced as an internal error, because sealing the
repository credential is the first thing that needs the key. If you provisioned
the KEK before this was fixed, re-run `create-kek.sh`; it reuses the existing
secret and grants the ACL.

The `cloud:` key is handled the same way: `openstack_auth_cloud` arrived in
2026.1, and `| default(omit, true)` sends the key where the variable exists and
omits it where it does not. Both shapes are what upstream's own
`service-ks-register` uses at the respective release. Supplying
`ferry_keystone_application_credential_id` yourself skips the whole path.

## What gets deployed

| container | group | what it is |
|---|---|---|
| `ferry_manager` | `ferry-manager` | control plane. API, scheduler, conductor, DR orchestration, the reconciler. Owns the database and the Keystone credential |
| `ferry_mover` | `ferry-mover` | data plane. librbd reads, chunking, encryption, repository I/O. Holds no database credential and never talks to OpenStack |
| `ferry_compute` | `ferry-compute` | quiesce agent. fsfreeze through libvirt's qemu-agent channel, and nothing else |

Three groups rather than one because they are three privilege levels, not three
processes. `inventory/ferry-groups` maps them onto `control`/`control`/`compute`
by default, which is the right answer for most clouds and the wrong one as soon
as movers should have their own blast radius.

## Install

```sh
./install.sh                       # finds kolla-ansible on PATH
```

Then, once:

```sh
# 1. inventory groups, as a second inventory rather than an edit to yours
cp inventory/ferry-groups /etc/kolla/ferry-groups

# 2. passwords
cat etc/passwords.yml >> /etc/kolla/passwords.yml
kolla-genpwd

# 3. globals: copy what you need out of etc/globals.yml
$EDITOR /etc/kolla/globals.yml

# 4. the KEK, in Barbican, by hand and on purpose (see below)
source /etc/kolla/admin-openrc.sh
./tools/create-kek.sh

# 5. Ceph: three cephx identities with a deliberate privilege split
./tools/create-cephx.sh
```

and then, as often as you like:

```sh
KOLLA=/usr/local/share/kolla-ansible/ansible/ferry.yml
kolla-ansible precheck -i /etc/kolla/inventory -i /etc/kolla/ferry-groups -p $KOLLA
kolla-ansible deploy   -i /etc/kolla/inventory -i /etc/kolla/ferry-groups -p $KOLLA
```

`reconfigure`, `upgrade`, `stop`, `pull` and `check` work the same way. Re-run
`install.sh` after upgrading kolla-ansible: pip replaces the share directory
and takes the symlinks with it.

## The KEK is not generated for you

Ferry encrypts every chunk under a per-domain DEK, and DEKs are only ever
stored wrapped under a KEK. The KEK therefore *is* the repository: lose
the bytes and every backup point in it is unreadable, permanently.

So two things this plugin will not do.

**It will not accept a KEK on local disk.** `ferry-manager` has a `file` KMS
driver and `precheck` refuses it. A file on one node has no replication, no
backup and no audit trail, and it dies with the node. Barbican and Vault are
the supported drivers, and a kolla cloud already has Barbican.

**It will not mint the key.** `tools/create-kek.sh` does, once, and prints the
secret id to put in `globals.yml`. A KEK reminted because a variable was empty
is a repository nobody can read, and creating the key that decrypts every
backup a tenant owns should leave a trail.

Barbican does not make the single key go away (that is what envelope
encryption is); it puts the key somewhere with real custody. The mitigation for
losing a whole cloud is a portable envelope: `ferry key export` writes a
domain key wrapped under a passphrase, with the repository and domain bound
into the AEAD, for import into another cloud's key manager.

## What the role does and does not touch

**Does.** `/etc/kolla/ferry-{manager,mover,compute}/` on the nodes; Ferry's
containers; the `ferry` database and its user; the `ferry` Keystone service,
endpoints and user; `haproxy/services.d/ferry-manager.cfg` and the proxysql
user and rules on the loadbalancer hosts. The restart that follows a changed
fragment is kolla's: `ferry.yml`'s loadbalancer play runs kolla's own
`loadbalancer` role and its container check around Ferry's fragments, exactly
as `site.yml` does for every service, so kolla's handlers restart haproxy:
backup nodes first, the VIP holder last, keepalived stopped and started around
it. That also means a Ferry run re-renders kolla's haproxy, keepalived and
proxysql configuration from `globals.yml`, as any `kolla-ansible deploy --tags
<service>` does.

**Does not.** Any other service's configuration, kolla's own certificates, or
the catalog entry of anything else. `manager.yaml` sets
`keystone.register_in_catalog: false` so Ferry does not fight
`service-ks-register` over its own endpoint URLs every fifteen minutes.

## Immutable backups

Object Lock is a property of the bucket, and the repository's **retention
contract** is how Ferry describes it. No plan asks for immutability and no policy
asserts it: a tenant is told backups cannot be erased before they expire because
the repository the policy writes to would not allow it.

Create the bucket with Object Lock enabled (versioning comes with it) and
register the contract with it: mode `compliance` or `governance`, a
`min_retention` floor, an optional ceiling, and a `soft_delete_grace`. OVH, MinIO
and RGW all honour it; the driver asks the bucket rather than trusting the flag,
and a write that asks for retention on a bucket that cannot lock fails the job
rather than storing an unprotected object.

The floor is a floor, not a retention. Every object is held until
`max(point expiry, written + min_retention)`, and a point's expiry is decided when
it **commits**, from the roles it won in its policy's ladder: the first point of
an anchored day is the daily, and if it also opens the week, month and year it
holds all four at once. So a daily point is locked for the daily's horizon, not
the yearly's, which is the one number that used to make a seven-year ladder cost
seven years for every point in it, chunks included. A chunk shared with an earlier
point still has its lock extended to the later date; what changed is that the
later date is now the right one.

Three things to plan for:

- A policy whose shortest tier (`keep.all`) is below the floor is **refused at
  creation, with the floor named**, rather than clamped.
- `point delete` soft-deletes: the point is hidden from the tenant and kept for
  `soft_delete_grace`, restorable by an operator, and a locked point is refused
  outright until its date passes.
- The contract is freely correctable until its `changeable_until` grace (three
  days after registration by default). After it the floor only increases, the mode
  is fixed, the block cannot be removed, and the repository cannot be unregistered
  while it still holds an object under lock.

`docs/kolla-ansible.md` has the operator runbook for registering one
and for writing the policy that writes to it.

## Authorization

Ferry ships a complete rule set compiled in, so a deployment that configures
nothing has every action covered. An action with no rule would deny on a working
endpoint and read like somebody's own override. The rules are oslo.policy-shaped,
so existing policy tooling and instincts transfer.

A file named by `policy.file` **overlays** that set action by action, which is
what makes one action separable without restating the rest. The protection-policy actions are
`policy:write`, `policy:assign`, `policy:shorten`, `repository:lock`,
`legal_hold:write`, `coverage:read`, `plan:pause:mandatory` and the vault action
`vault:write`, all shipped as `rule:system_admin`, and `policy:list` and
`vault:list` as `rule:project_reader`. A tenant sees only what the handler
renders, which is sentences and never a repository name.

The one override a deployment is likely to want is retention reductions behind a
second role, like Cohesity's Data Security role or Veeam's four-eyes:

```yaml
"policy:shorten": "rule:system_admin and role:data_security"
"repository:lock": "rule:system_admin and role:data_security"
"legal_hold:write": "rule:system_admin and role:data_security"
```

The role renders no `policy:` section, so reaching it from a kolla deploy takes
two steps of your own: mount the file with `ferry_manager_extra_volumes`, and set
`policy.file` in `/etc/kolla/config/ferry/manager.yaml`, which is merged over the
role's template. Two warnings. A `policy.file` naming a path that does not exist
inside the container is a manager that **refuses to start**, so place the file
first. Each manager re-reads the file every `policy.reload_interval` (a minute by
default), so an edit is in force within that time without a restart. A file that
no longer parses is refused whole and logged once, and the rules already loaded
stay in force until it is fixed.

`plan:pause:mandatory` is a new gate on an existing operation. A deployment that
has relaxed `plan:update` or `plan:delete` in its own file still has the mandatory
path refused, because the handler consults a second action.

## Logs

Every record goes to stderr, so `docker logs ferry_manager` works, **and** to
`/var/log/kolla/ferry/ferry-{manager,mover,compute}.log` on the `kolla_logs`
volume, which is where kolla's fluentd reads every service's logs from. The
file is JSON lines, and Ferry rotates it itself (`ferry_log_file_max_mib`,
`ferry_log_file_keep`, 50 MiB × 6 by default) because kolla's logrotate names
the services it rotates and an out-of-tree one is not on that list.

fluentd's own input tails a fixed list of OpenStack directories, so Ferry ships
an input and a filter for kolla's custom-config mechanism:

```sh
cp etc/fluentd/input/20-ferry.conf  /etc/kolla/config/fluentd/input/
cp etc/fluentd/filter/20-ferry.conf /etc/kolla/config/fluentd/filter/
kolla-ansible reconfigure -i /etc/kolla/inventory --tags common
```

The records then land in OpenSearch under `programname` `ferry-manager`,
`ferry-mover` or `ferry-compute`, with `log_level` and `Payload` like every
other service and Ferry's own keys (`job_id`, `request_id`, …) alongside.
`ferry_log_file_enabled: false` turns the file off.

## Ports

| port | what | exposure |
|---|---|---|
| 9776 | tenant/operator API and the SPA, Keystone-authenticated | haproxy, internal and external |
| 9777 | mover pull protocol, mTLS | haproxy in **TCP** mode, internal only |
| 9779 | quiesce API, mTLS | node address, dialled by managers only |
| 9775 / 9782 / 9780 | admin: `/metrics`, `/healthz`, `/readyz` | loopback |

**Scrape 9775, not 9776.** `9776/metrics` used to need no token and now needs a
system-scoped Keystone one: the manager's registry carries per-plan gauges
labelled with other tenants' plan ids and byte counts, and the public listener
was answering them to anybody who asked. Prometheus cannot obtain or refresh a
Keystone token, so the intended scrape target is and remains the loopback admin
listener on 9775, which is unauthenticated because nothing off the node can
reach it. A job pointed at 9776 starts getting 401s the moment the manager is
upgraded.

9777 is TCP-mode rather than HTTP because it requires a client certificate and
binds `mover_id` to its common name: haproxy has to pass the TLS session
through, not terminate it. It carries unwrapped DEKs and is never
token-authenticated, which is why it is never external.

## PKI

Ferry needs a private CA that issues *client* identities, which kolla's
certificates are not: kolla's internal certificate is a server certificate for
the VIP. So the role runs its own, on the deploy host, under
`/etc/kolla/config/ferry/pki/`:

- one CA, reused forever: an existing one is never reminted, so re-running
  does not invalidate deployed certificates;
- one server certificate for the mover API, naming the VIP and every manager;
- one client certificate **per mover**, whose CN is that mover's `mover_id`.
  The manager refuses a claim whose `mover_id` is not the CN of the certificate
  that carried it, so a mover cannot claim another mover's jobs and be handed
  its keys;
- one server certificate per compute node, whose CN is the name Nova reports
  for that hypervisor, because that is the name the manager dials;
- one client certificate shared by the managers for dialling those agents.

Adding a mover or a hypervisor and re-running mints only the new leaf, except
the mover API's certificate, which is reissued because its SAN list has to
grow.

Set `ferry_pki_generate: false` to provide all of this yourself.

## Ceph

Ferry reads production RBD images directly; there is no Cinder or Nova API in
the data path. `tools/create-cephx.sh` writes `ceph.conf` and three
keyrings into `/etc/kolla/config/ferry/ceph/`, and the role distributes each
one only to the component entitled to it: the manager's key never lands in the
mover's container, and the restore key is the only one that can write a live
volume.

`ferry_ceph_enabled: false` is available for a control-plane smoke test on a
cloud with no Ceph. The manager comes up, serves its API and appears in the
catalog; every plan is then refused at creation, because discovery cannot prove
a resource is RBD-backed.

## Notes for the curious

**Why the playbook lives inside kolla-ansible's `ansible/` directory.** Ansible
resolves `filter_plugins`, `action_plugins`, `library` modules and
`group_vars` relative to the playbook. A playbook anywhere else gets no
`kolla_address`, no `merge_yaml`, no `kolla_container` and none of kolla's
`group_vars/all`, which is most of what the role is written against.

**Why Ferry ships no `group_vars` fragment.** It used to, symlinked beside
kolla's own `group_vars/all/ferry.yml`. That only works from 2025.2, where
`group_vars/all` is a directory; on 2025.1 and earlier it is the single file
`all.yml`, and putting a directory next to it does not merge: ansible's
`DataLoader.find_vars_files` breaks on the first hit and checks the directory
form first, so kolla's entire `all.yml` would be loaded *not at all*. The
defaults live in the role instead, and `ferry.yml` spells `enable_ferry |
default(false)` in the two places that run before any role.

**Why `service-check-containers` is not used.** Its `compare_container` execs
`/usr/local/bin/kolla_set_configs --check` inside the container to decide
whether config is stale. Ferry's images are a Go binary on plain Ubuntu or
Debian, with no `kolla_start` and no `set_configs`, so that exec errors rather
than reporting a difference. `check-containers.yml` uses `start_container`
instead, and config changes are noticed where they happen: `config.yml`
notifies the restart handlers when a rendered file moves.

**Why the configs are `merge_yaml` and not `template`.** They are YAML, so an
operator overriding one key should not have to restate the file. Drop overrides
in `/etc/kolla/config/ferry/{manager,mover,compute}.yaml`, or
`/etc/kolla/config/ferry/<host>/…` for one host. Configuration decoding rejects
unknown keys, so a typo there is a component that will not start rather than a
setting that quietly did nothing.
