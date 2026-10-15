# Changelog

What changed for operators in each release of the deployment manifests. Tags
match Ferry releases, and each release pins the images of the same version.

## [0.17.0] - 2026-10-15

First public release of the deployment manifests: the kolla-ansible role, the
Helm chart, the Juju charms, the alert rules and the guides.

### Added

- `FerryReferencedObjectMissing` (critical): a repository is missing an object a
  backup point references. It fires on the first missing object, including one
  counted by a series that did not exist an hour earlier. The kolla-ansible guide
  has a runbook for it.
- `monitoring/ferry.rules` is the one source of the alert rules, with promtool unit
  tests. `make rules` writes the copies each deployer ships, and CI fails if they
  differ.
- Support bundles: `ferry admin support-bundle` builds one archive for diagnosing
  a problem, with credentials and tenant data left out. New manager settings
  `support.deadline`, `support.alertmanager_url`, `support.alertmanager_username`
  and `support.alertmanager_password`.

### Upgrade notes

- The manager migrates the schema to 63 on start. Managers roll before movers and
  agents, as with every release.
- kolla-ansible: `deploy` rewrites `ferry.rules` in the node custom config, but
  Prometheus loads it only on `kolla-ansible reconfigure -t prometheus`. Run it
  after the upgrade to load the new alert.

[0.17.0]: https://github.com/osie/ferry-deploy/releases/tag/v0.17.0
