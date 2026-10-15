SHELL := /usr/bin/env bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help

PRESETS := $(sort $(wildcard helm/ferry/presets/*.yaml))
RULE_COPIES := helm/ferry/files/ferry.rules \
	juju/ferry/src/prometheus_alert_rules/ferry.rules \
	kolla-ansible/ansible/roles/ferry/files/prometheus/ferry.rules
KUBERNETES_VERSION ?= 1.30.0
CRD_SCHEMAS := https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json
NO_PUBLIC_SCHEMA := OpenStackDataPlaneService,KeystoneService,Route
HELM_SET := --set cloud.name=ci --set api.ingress.host=ferry.example.com
VENV ?= .venv
VERSION := $(shell sed -n 's/^version: *//p' helm/ferry/Chart.yaml)

.PHONY: help ci test lint rules check-rules test-rules test-helm test-juju test-kolla-ansible \
	check-pins yamllint shellcheck codespell links actionlint package clean

help: ## List the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-20s %s\n", $$1, $$2}'

ci: lint test test-kolla-ansible ## Everything CI runs

test: check-rules test-rules check-pins test-helm test-juju ## Every test but the kolla-ansible install

lint: yamllint shellcheck codespell links actionlint ## Every linter

rules: ## Copy monitoring/ferry.rules over every deployer's copy
	@for f in $(RULE_COPIES); do cp monitoring/ferry.rules "$$f"; echo "wrote $$f"; done

check-rules: ## Every deployer ships monitoring/ferry.rules, and every alert is documented
	@for f in $(RULE_COPIES); do \
		cmp -s monitoring/ferry.rules "$$f" || { echo "$$f differs from monitoring/ferry.rules: run make rules" >&2; exit 1; }; \
	done
	@for a in $$(sed -n 's/^ *- alert: *//p' monitoring/ferry.rules); do \
		grep -q "\`$$a\`" monitoring/README.md || { echo "monitoring/README.md does not describe $$a" >&2; exit 1; }; \
	done
	@echo "rule copies match, every alert is documented"

test-rules: ## promtool checks and unit-tests the alert rules
	promtool check rules monitoring/ferry.rules
	cd monitoring && promtool test rules ferry.rules.test.yml

test-helm: ## Lint and render the chart with every preset, validate it, run the chart tests
	@for p in $(PRESETS); do \
		echo "== $$p"; \
		helm lint helm/ferry --quiet -f "$$p" $(HELM_SET); \
		helm template t helm/ferry -n openstack -f "$$p" $(HELM_SET) | \
			kubeconform -strict -summary -kubernetes-version $(KUBERNETES_VERSION) \
				-schema-location default -schema-location '$(CRD_SCHEMAS)' -skip $(NO_PUBLIC_SCHEMA); \
	done
	cd helm && CI=true go test ./...

$(VENV)/.juju: juju/ferry/requirements.txt juju/ferry-compute/requirements.txt
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install --quiet 'ops[testing]' pytest -r juju/ferry/requirements.txt -r juju/ferry-compute/requirements.txt
	touch $@

test-juju: $(VENV)/.juju ## Run both charms' unit tests
	@for c in ferry ferry-compute; do echo "== juju/$$c"; (cd "juju/$$c" && "$(abspath $(VENV))/bin/python" -m pytest -q tests); done

test-kolla-ansible: ## Syntax-check the playbook inside a real kolla-ansible install
	tests/kolla-ansible-syntax.sh

check-pins: ## The chart, the charm and the role pin the same release (and TAG, when set)
	@chart=$$(sed -n 's/^version: *//p' helm/ferry/Chart.yaml); \
	app=$$(sed -n 's/^appVersion: *"\{0,1\}\([^"]*\)"\{0,1\}/\1/p' helm/ferry/Chart.yaml); \
	images=$$(sed -n 's/^  \(manager\|mover\|compute\): .*:\(.*\)$$/\2/p' helm/ferry/values.yaml | sort -u); \
	charm=$$(awk '/^    version:$$/{f=1; next} f && /^      default:/{gsub(/"/,"",$$2); print $$2; exit} f && /^    [a-z]/{exit}' juju/ferry/charmcraft.yaml); \
	role=$$(sed -n 's/^ferry_tag: *"\(.*\)"/\1/p' kolla-ansible/ansible/roles/ferry/defaults/main.yml); \
	want=$${TAG:-$$chart}; want=$${want#v}; \
	for pair in "chart version:$$chart" "chart appVersion:$$app" "chart images:$$images" "charm version:$$charm" "kolla-ansible ferry_tag:$$role"; do \
		name=$${pair%%:*}; got=$${pair#*:}; \
		[ "$$got" = "$$want" ] || { echo "$$name is '$$got', want $$want" >&2; exit 1; }; \
	done; \
	echo "every pin names $$want"

yamllint: ## Lint every YAML file
	yamllint -s .

shellcheck: ## Lint every shell script
	shellcheck kolla-ansible/install.sh kolla-ansible/tools/*.sh tests/*.sh

codespell: ## Check spelling
	codespell

links: ## Check every relative link and anchor in the markdown
	lychee --offline --include-fragments --no-progress './**/*.md'

actionlint: ## Lint the GitHub workflows
	actionlint

package: ## Build the release artefacts into dist/
	rm -rf dist && mkdir -p dist
	helm package helm/ferry -d dist
	tar -czf dist/ferry-kolla-ansible-$(VERSION).tar.gz --exclude='.venv*' kolla-ansible
	cp monitoring/ferry.rules dist/ferry-$(VERSION).rules
	cd dist && sha256sum * > SHA256SUMS
	@ls -l dist

clean: ## Remove build output and virtualenvs
	rm -rf dist $(VENV) .venv-kolla-ansible helm/ferry/charts
