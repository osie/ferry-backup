# Security

## Reporting a vulnerability

Please do not open a public issue for a security problem. Report it privately
through GitHub: open the repository's **Security** tab and choose **Report a
vulnerability**, or go to <https://github.com/osie/ferry-backup/security/advisories/new>.

Include what is affected (a manifest, a default, a Ferry component), the versions,
and how to reproduce it. We aim to acknowledge reports within three working days
and keep you informed until a fix is released. We credit reporters in the advisory
unless you ask us not to.

Vulnerabilities in Ferry itself, rather than in these manifests, are welcome
through the same channel; we route them to the right place.

## Supported versions

Fixes are made in the latest release. Upgrade to it to receive them.

## What the manifests are responsible for

Ferry handles credentials for the cloud's database, Keystone, Ceph and backup
repositories, and runs an agent on every hypervisor. The manifests decide where
each of those credentials is mounted and which network each listener binds to,
so these are in scope:

- a credential reaching a component that should not hold it (for example, a
  database or Keystone credential in the mover, or a repository credential on a
  hypervisor);
- a privileged listener (the admin port, which serves `/loglevel` and quota
  figures) exposed beyond the network the guides say it belongs on;
- a default that weakens Ferry's mTLS or Ceph privilege split;
- a secret written to a log, an image layer or a world-readable file.
