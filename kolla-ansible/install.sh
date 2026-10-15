#!/usr/bin/env bash
# Links Ferry's role, playbook and globals into an existing kolla-ansible
# installation.
#
# Symlinks rather than copies, so editing this repository is editing what
# ansible runs. The links point back here; moving or deleting this checkout
# breaks the plugin, which is the honest failure — a stale copy that silently
# disagrees with the tree is worse.
#
# WHY it has to go inside kolla-ansible's own ansible/ directory: ansible
# resolves group_vars, filter_plugins, action_plugins and library modules
# relative to the playbook. A playbook outside that directory gets no
# kolla_address, no merge_yaml, no kolla_container and none of kolla's
# group_vars/all — which is most of what the role is written against.
#
# Re-run this after upgrading kolla-ansible: pip replaces the share directory
# and takes the symlinks with it.
#
#   ./install.sh                      # find kolla-ansible on PATH
#   ./install.sh /path/to/share/kolla-ansible
#   ./install.sh --uninstall
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)

uninstall=false
target=${1:-}
if [ "${target}" = "--uninstall" ]; then
	uninstall=true
	target=${2:-}
fi

# kolla-ansible's data files live in <prefix>/share/kolla-ansible. The CLI is
# in <prefix>/bin, which is the reliable way to find the prefix for a venv, a
# --user install or a distro package alike.
if [ -z "$target" ]; then
	cli=$(command -v kolla-ansible || true)
	[ -n "$cli" ] || {
		echo "kolla-ansible is not on PATH; pass the share directory explicitly:" >&2
		echo "  $0 /path/to/share/kolla-ansible" >&2
		exit 1
	}
	prefix=$(cd "$(dirname "$(readlink -f "$cli")")/.." && pwd)
	target="$prefix/share/kolla-ansible"
fi

ANSIBLE_DIR="$target/ansible"
[ -d "$ANSIBLE_DIR/roles" ] || {
	echo "$ANSIBLE_DIR does not look like a kolla-ansible installation" >&2
	exit 1
}

link() { # link <source> <destination>
	local src=$1 dst=$2
	if [ -e "$dst" ] && [ ! -L "$dst" ]; then
		echo "refusing to replace $dst: it is not a symlink" >&2
		exit 1
	fi
	rm -f "$dst"
	ln -s "$src" "$dst"
	echo "  $dst -> $src"
}

unlink_if_ours() { # unlink_if_ours <destination>
	local dst=$1
	if [ -L "$dst" ] && [[ "$(readlink "$dst")" == "$HERE"/* ]]; then
		rm -f "$dst"
		echo "  removed $dst"
	fi
}

if $uninstall; then
	echo "removing Ferry from $ANSIBLE_DIR"
	unlink_if_ours "$ANSIBLE_DIR/roles/ferry"
	unlink_if_ours "$ANSIBLE_DIR/ferry.yml"
	unlink_if_ours "$ANSIBLE_DIR/group_vars/all/ferry.yml"
	echo "done. Containers and configuration on the nodes are untouched;"
	echo "run 'kolla-ansible stop -p .../ferry.yml' before this if you meant to remove those too."
	exit 0
fi

echo "installing Ferry into $ANSIBLE_DIR"
link "$HERE/ansible/roles/ferry"          "$ANSIBLE_DIR/roles/ferry"
link "$HERE/ansible/ferry.yml"            "$ANSIBLE_DIR/ferry.yml"
# No group_vars link. Ferry's defaults used to be a fragment symlinked into
# kolla's own ansible/group_vars/all/, which works only from kolla-ansible
# 2025.2 — before that `group_vars/all` is the single file all.yml, and creating
# a directory beside it does not merge: ansible loads the directory INSTEAD of
# all.yml and every kolla variable silently disappears. The defaults now live in
# the role, and ferry.yml carries `| default(false)` for the one variable a
# role default cannot reach.
if [ -L "$ANSIBLE_DIR/group_vars/all/ferry.yml" ]; then
	echo "  removing the obsolete group_vars link from an older install"
	unlink_if_ours "$ANSIBLE_DIR/group_vars/all/ferry.yml"
fi

cat <<EOF

installed. Next:

  1. Inventory groups — Ferry needs ferry-manager, ferry-mover and
     ferry-compute. Either append kolla-ansible/inventory/ferry-groups to your
     inventory, or pass it as a second inventory:

       cp $HERE/inventory/ferry-groups /etc/kolla/ferry-groups

  2. Passwords — append the keys Ferry needs, then fill the blanks:

       cat $HERE/etc/passwords.yml >> /etc/kolla/passwords.yml
       kolla-genpwd

  3. Globals — copy the settings you need into /etc/kolla/globals.yml:

       $HERE/etc/globals.yml

  4. The KEK — provisioned once, by hand, on purpose:

       source /etc/kolla/admin-openrc.sh
       $HERE/tools/create-kek.sh

  5. Ceph — three cephx identities with a deliberate privilege split:

       $HERE/tools/create-cephx.sh

  6. Logs — so kolla's fluentd ships Ferry's like every other service's:

       cp $HERE/etc/fluentd/input/20-ferry.conf  /etc/kolla/config/fluentd/input/
       cp $HERE/etc/fluentd/filter/20-ferry.conf /etc/kolla/config/fluentd/filter/
       kolla-ansible reconfigure -i /etc/kolla/inventory --tags common

  7. Deploy:

       kolla-ansible precheck -i /etc/kolla/inventory -i /etc/kolla/ferry-groups \\
         -p $ANSIBLE_DIR/ferry.yml
       kolla-ansible deploy   -i /etc/kolla/inventory -i /etc/kolla/ferry-groups \\
         -p $ANSIBLE_DIR/ferry.yml
EOF
