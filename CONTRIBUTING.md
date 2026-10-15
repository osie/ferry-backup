# Contributing

Thank you for helping. Bug reports, fixes for a distribution we have not tested,
and corrections to the guides are all welcome.

## How this repository is maintained

The manifests are developed alongside Ferry itself and exported here with each
Ferry release, so a tag in this repository always matches the images it pins. A
pull request is reviewed here as usual; once accepted, it is applied to the
development tree and arrives back in this repository with the next export, under
your authorship. The pull request is closed with a link to the release that
carries it.

## Reporting a bug

Open an issue with the bug template. The most useful things to include:

- the deployer (kolla-ansible, Helm with which preset, or Juju) and its version;
- the Ferry version (`ferry --version`, or the image tag);
- the OpenStack release and distribution;
- what you ran, what happened, and what you expected;
- the `summary.md` from a [support bundle](docs/support-bundle.md), or the whole
  bundle if you are comfortable sharing it. Bundles leave credentials and tenant
  data out; `--hash-names` hashes project, plan and bucket names too.

Security issues go through [SECURITY.md](SECURITY.md), never a public issue.

## Making a change

You need Helm 3.18+, Go 1.23+, Python 3.10+, `promtool`, `shellcheck` and
`yamllint`. Then:

```sh
make test   # chart rendering, charm unit tests, alert rule tests, rule copies
make lint   # yamllint, shellcheck, markdown links, spelling
```

CI runs the same targets, plus a syntax check of the kolla-ansible playbook
against a real kolla-ansible install (`make test-kolla-ansible`, which creates a
virtualenv).

Some things to keep in mind:

- **The three deployers move together.** A setting added to one usually belongs
  in the other two, and the guides say what each deployer does with it.
- **Edit alert rules in [`monitoring/ferry.rules`](monitoring/ferry.rules) only**,
  run `make rules` to update the deployers' copies, and add a test to
  `monitoring/ferry.rules.test.yml` that fails without your change.
- **Image pins change only with a release.** The chart's `appVersion`, the
  charm's `version` default and the role's `ferry_tag` always name the same
  release, and CI checks they agree with the tag.
- **No credentials, hostnames or addresses from a real cloud**, in code,
  examples or tests. Use `example.com`, the documentation address ranges and
  obviously fake values.

## Commits and sign-off

Write commit messages that say what changed and why. Sign off each commit to
certify the [Developer Certificate of Origin](https://developercertificate.org/):

```sh
git commit -s
```

By contributing you agree that your contribution is licensed under the
[Apache License, Version 2.0](LICENSE).
