# Monitoring

[`ferry.rules`](ferry.rules) is the one source of Ferry's Prometheus alert rules.
Each deployer ships a copy, and `make rules` writes them:

| deployer | copy | how it is loaded |
|---|---|---|
| kolla-ansible | `kolla-ansible/ansible/roles/ferry/files/prometheus/ferry.rules` | with `ferry_prometheus_integration: true`, into kolla's Prometheus on the next `kolla-ansible reconfigure -t prometheus` |
| Helm | `helm/ferry/files/ferry.rules` | as a `PrometheusRule` for the Prometheus Operator |
| Juju | `juju/ferry/src/prometheus_alert_rules/ferry.rules` | through COS, over the `metrics-endpoint` relation |

CI fails if a copy differs from the source.

## Alerts

| alert | severity | fires when |
|---|---|---|
| `FerryTargetDown` | warning | a manager, mover or agent has not answered on its admin port for 10 minutes |
| `FerryNoMoverAlive` | critical | no mover has checked in for 10 minutes, so nothing is being backed up |
| `FerryDispatchPaused` | warning | backup dispatch has been paused for 30 minutes; the labels name the watch holding it and why |
| `FerryRepositoryDown` | critical | a repository has failed its health probe for 10 minutes |
| `FerryJobDeadLettered` | warning | jobs gave up in the last day; the labels give the kind and the error code |
| `FerryBucketRemovalGivenUp` | warning | a failed bucket restore still holds a bucket whose removal job gave up |
| `FerryPlanOverdue` | warning | a plan has resources past their cadence for 30 minutes |
| `FerryPlanUnsatisfied` | warning | a project has plans whose policy cannot be met, for an hour |
| `FerryGCDegraded` | warning | a repository's sweep cannot enumerate every live point, so it reclaims nothing |
| `FerryGCStale` | warning | a repository has not completed a sweep in three days |
| `FerryGuestStuckFrozen` | critical | a guest is still frozen past its deadline |
| `FerryThawFailed` | critical | a guest thaw failed |
| `FerryMissedTicks` | warning | scheduled backups were missed in the last hour |
| `FerryLoopErrors` | warning | a manager loop failed more than five times in 15 minutes |
| `FerryMoverCannotReachRepository` | warning | movers handed back more than two jobs in an hour because they could not reach a repository |
| `FerryReferencedObjectMissing` | critical | a repository is missing an object a backup point references |

[The kolla-ansible guide](../docs/kolla-ansible.md#when-ferryreferencedobjectmissing-fires)
has the runbook for `FerryReferencedObjectMissing`.

## Tests

[`ferry.rules.test.yml`](ferry.rules.test.yml) holds unit tests for the rules,
run by `promtool test rules` in CI and by `make test-rules`. A change to a rule
comes with a test that fails without it.
