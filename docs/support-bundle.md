# Support bundles

A support bundle is one tar.gz archive an operator sends when asking for help. It is built so that
someone who has nothing but the archive can diagnose the problem: the service's records for a
window of time, every manager's, mover's and compute agent's version, checks, warnings and errors,
and a page of findings worked out in advance. Credentials, keys and the content of backups are never
collected, and every file is scanned for secrets before the archive is kept.

## Building one

```
ferry admin support-bundle                       # the last 72 hours, everything
ferry admin support-bundle --since 144h          # a longer window, up to 1080h
ferry admin support-bundle --job JOB_ID          # one job, with its plan and points
ferry admin support-bundle --project PROJECT_ID  # one project's records
ferry admin support-bundle --dry-run             # what would be collected, with row counts
ferry admin support-bundle --hash-names          # names replaced with hashes
```

It needs an operator token (a system-scoped admin; the policy rule is `support:bundle`). The
manager asks every component for its own report and waits up to `support.deadline` (90 seconds by
default) for them, and up to a minute longer for a manager replica still writing its own, then builds
the archive and keeps it for 24 hours. The command polls until the
archive is ready, downloads it, checks its SHA-256 against the bundle's record and writes it with
mode 0600. One bundle builds at a time per site.

A scope (`--job`, `--plan`, `--project`) narrows the tenant records: jobs, events, points, plans,
restores, usage. The fleet, Ceph, the logs and the repositories stay whole, because the cause of a
failed job is often somewhere else.

## When the manager is down

The same bundle can be built without a running manager. It reads the database and the node's own
log files directly and asks the compute agents itself. Movers only report through a running manager,
so their side comes from the movers table, the job events and the manager logs. It reads and never
writes.

```
ferry-manager support-bundle --config /etc/ferry/manager.yaml --since 72h --out /tmp/support.tar.gz
```

Run it where the manager's configuration and log files are:

- kolla-ansible: `docker exec ferry_manager ferry-manager support-bundle --out /var/log/kolla/ferry/support.tar.gz`,
  then copy the file from the host's `kolla_logs` volume.
- Helm: `kubectl exec deploy/<release>-manager -- ferry-manager support-bundle --out /tmp/support.tar.gz`,
  then `kubectl cp`.
- Juju: `juju ssh ferry/0` and run it in the manager's container.

## What is in it

| path | what |
|---|---|
| `README.md` | what Ferry is, every file, how to join records, a glossary of states |
| `summary.md`, `summary.json` | findings: failed jobs, pauses, hand-backs by mover, RPO misses, version skew, silent components, failing checks |
| `manifest.json` | what was collected and from where, what could not be and why, every truncation, the number of redactions |
| `data/*.jsonl` | the service's records for the window, one JSON object per line, with full ids and UTC timestamps |
| `components/*.json` | each component's build, redacted effective configuration, self-checks and state |
| `logs/<component>/<instance>.jsonl` | warnings, errors and every line but routine access logging in the window; `.tail.jsonl` is the newest lines unfiltered |
| `metrics/*.prom` | a scrape of each component's metrics at collection time |
| `ceph/*.json` | `ceph status`, `health detail` and `osd df` as the building manager saw them |
| `catalog/error-codes.json` | the meaning and next step of every error and event code |
| `alerts.json` | firing alerts, when `support.alertmanager_url` is set |

Logs are read from each component's configured log file, across its rotated copies, so a window
reaches back as far as the files do. A component without a log file reports its newest lines from
memory instead. A message repeated more than 100 times in the window keeps its first and last 50
occurrences, and the component's report counts the rest under `repeated`. Dispatch pauses and
resumes, repository health changes, restarts and hand-backs are always kept, however much else the
window holds.

Self-checks: a manager resolves and connects to Keystone and every repository endpoint, reads its
database and asks Ceph for its health. A mover does the same for the manager and the repositories it
has opened, and asks the Ceph cluster its last work order read. A compute agent connects to libvirt.
Every report lists the resolvers in `/etc/resolv.conf`, and the manifest gives each component's
clock skew against the manager.

## What is never in it

- Encryption keys and key-manager material, repository and S3 credentials, Keystone passwords and
  tokens, application credentials, database passwords, and the password part of any URL.
- Chunk content, file-level restore paths, bucket object keys, job payloads, and labels tenants put
  on plans.
- Libvirt domain XML.

Every table is read column by column from a list; a column added to the schema is not collected
until someone decides it should be. For jobs of buckets, shares, file-level restore and attach, the
free text of errors and events is withheld and the codes stay. Log attributes named `key`,
`object`, `path`, `file`, `prefix` or `marker` are redacted, as is any attribute whose name says it
is a credential.

Each component scans its own report for the secrets it holds before sending it, and withholds
everything but its identity and checks if one is found. The manager scans the finished archive for
private keys, tokens, URLs and DSNs carrying passwords, credential fields, and the exact values of
every secret in its configuration. A match fails the bundle and names the rule, never the secret.

With `--hash-names`, project, plan, protection group, member and bucket names become salted hashes
(`h:` and 12 hex digits), the same name hashing the same way within one bundle. IDs are kept, so
records still join. The command writes the mapping next to the archive as `<file>.names.json`: keep
it and do not send it.

## Configuration

| key | default | |
|---|---|---|
| `support.deadline` | `90s` | how long a bundle waits for reports |
| `support.alertmanager_url` | empty | Alertmanager whose firing alerts a bundle includes |
| `support.alertmanager_username`, `support.alertmanager_password` | empty | basic auth for it |
| `loops.support` | every 5s | how often a manager replica looks for a bundle to answer |
| `loops.replica_beat` | every 30s | how often a manager replica records itself in `manager_replicas` |
