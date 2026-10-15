#!/usr/bin/env bash
# Syntax-checks the playbook inside a real kolla-ansible install, linked in by install.sh.
set -euo pipefail

here=$(cd "$(dirname "$0")/.." && pwd)
venv=${VENV:-$here/.venv-kolla-ansible}
series=${KOLLA_ANSIBLE_SERIES:-20}

if [ ! -x "$venv/bin/kolla-ansible" ]; then
	python3 -m venv "$venv"
	"$venv/bin/pip" install --quiet --upgrade pip
	"$venv/bin/pip" install --quiet "kolla-ansible==${series}.*"
fi
export PATH="$venv/bin:$PATH"

share="$venv/share/kolla-ansible"
kolla-ansible install-deps >/dev/null

"$here/kolla-ansible/install.sh" "$share"
trap '"$here/kolla-ansible/install.sh" --uninstall "$share" >/dev/null' EXIT

work=$(mktemp -d)
trap '"$here/kolla-ansible/install.sh" --uninstall "$share" >/dev/null; rm -rf "$work"' EXIT
cp "$share/ansible/inventory/multinode" "$work/multinode"
cp "$here/kolla-ansible/inventory/ferry-groups" "$work/ferry-groups"

echo "kolla-ansible $(pip show kolla-ansible 2>/dev/null | sed -n 's/^Version: //p'), ansible-core $(ansible --version | sed -n '1s/.*core \([0-9.]*\).*/\1/p')"
ansible-playbook -i "$work/multinode" -i "$work/ferry-groups" --syntax-check "$share/ansible/ferry.yml"
for g in ferry-manager ferry-mover ferry-compute; do
	n=$(ansible-inventory -i "$work/multinode" -i "$work/ferry-groups" --graph "$g" 2>/dev/null | grep -c -- '--[a-z0-9]' || true)
	[ "$n" -gt 0 ] || { echo "the example inventory puts no host in $g" >&2; exit 1; }
	echo "$g: $n hosts in kolla's example inventory"
done
echo "ferry.yml passes the syntax check against kolla-ansible's own tree"
