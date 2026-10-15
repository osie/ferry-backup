#!/usr/bin/env bash
# Mints Ferry's three cephx identities and writes them where the kolla role
# expects to find them.
#
# Three, not one, because privilege is deliberately split by what each component may do:
#
#   client.ferry-manager   snapshots production, rotates anchors, polls
#                          `rbd trash ls` for the reconciler. Only the
#                          manager ever holds it.
#
#   client.ferry-mover     READ-ONLY on production, read-write on Ferry's own
#                          pools. Named by every backup, verify, GC and tiering
#                          work order. The mover is the component that runs
#                          unattended holding unwrapped encryption keys, so it
#                          is the one that must not be able to write a tenant's
#                          disk.
#
#   client.ferry-restore   read-write on the volume and VM pools. A restore
#                          writes chunks INTO a live volume's RBD image, which
#                          is exactly what the mover's key cannot do, so it
#                          carries its own identity — and only a restore work
#                          order ever carries it.
#
# Run on a host with an admin ceph keyring. Idempotent: `ceph auth get-or-create`
# returns the existing key rather than rotating it.
set -euo pipefail

OUT=${FERRY_CEPH_CONFIG_DIR:-/etc/kolla/config/ferry/ceph}

# Match these to the cloud. They are the same names the role's
# ferry_ceph_*_pools variables carry, and a mismatch is a refused job rather
# than a permission error found mid-backup.
VOLUMES=${FERRY_VOLUME_POOL:-volumes}
VMS=${FERRY_VM_POOL:-vms}
IMAGES=${FERRY_IMAGE_POOL:-images}
REPO=${FERRY_REPO_POOL:-ferry-repo}
TIER=${FERRY_TIER_POOL:-ferry-tier}
CINDER=${FERRY_CINDER_USER:-cinder}

command -v ceph >/dev/null || {
	echo "ceph client not found; run this on a host with an admin keyring" >&2
	exit 1
}

mkdir -p "$OUT"
umask 077

echo "== ceph.conf =="
if [ -f "$OUT/ceph.conf" ]; then
	echo "  $OUT/ceph.conf: present, left alone"
else
	# Only the mon addresses and the fsid are needed; the mover and manager are
	# clients, not daemons.
	ceph config generate-minimal-conf > "$OUT/ceph.conf"
	echo "  $OUT/ceph.conf: written"
fi

# key <name> <caps...>
key() {
	local name=$1
	shift
	ceph auth get-or-create "client.$name" "$@" > "$OUT/ceph.client.$name.keyring"
	echo "  client.$name"
}

echo "== identities =="

# The manager snapshots and enumerates. It needs to create and remove snapshots
# on production images and to read the trash namespace, so it is rw there.
key ferry-manager \
	mon "profile rbd" \
	osd "profile rbd pool=$VOLUMES, profile rbd pool=$VMS, profile rbd-read-only pool=$IMAGES, profile rbd pool=$REPO, profile rbd pool=$TIER"

# The mover reads production and writes only Ferry's own pools. This asymmetry
# is the whole point of there being three keys.
key ferry-mover \
	mon "profile rbd" \
	osd "profile rbd-read-only pool=$VOLUMES, profile rbd-read-only pool=$VMS, profile rbd-read-only pool=$IMAGES, profile rbd pool=$REPO, profile rbd pool=$TIER"

# Restore writes into a live volume, which nothing else here may do.
key ferry-restore \
	mon "profile rbd" \
	osd "profile rbd pool=$VOLUMES, profile rbd pool=$VMS, profile rbd-read-only pool=$IMAGES, profile rbd pool=$TIER"

echo "== client.$CINDER and the tier =="
cinder_caps=$(ceph auth get "client.$CINDER" 2>/dev/null | sed -n 's/^[[:space:]]*caps \([a-z]*\) = "\(.*\)"$/\1 \2/p' || true)
if [ -z "$cinder_caps" ]; then
	echo "  client.$CINDER not found. Set FERRY_CINDER_USER to the key Cinder and Nova attach RBD volumes"
	echo "  with, and give it 'profile rbd-read-only pool=$TIER' alongside what it has."
elif printf '%s\n' "$cinder_caps" | grep -Eq "^osd .*pool=$TIER(,| |\$)"; then
	echo "  client.$CINDER can read $TIER"
else
	grant="ceph auth caps client.$CINDER"
	osd_seen=no
	while read -r kind caps; do
		if [ "$kind" = osd ]; then
			caps="$caps, profile rbd-read-only pool=$TIER"
			osd_seen=yes
		fi
		grant="$grant $kind '$caps'"
	done <<<"$cinder_caps"
	[ "$osd_seen" = yes ] || grant="$grant osd 'profile rbd-read-only pool=$TIER'"
	echo "  client.$CINDER has no caps on $TIER. An instant restore is a clone of a snapshot there,"
	echo "  and until Ferry flattens it Cinder cannot adopt it and Nova cannot attach it. Grant it with"
	echo "  (ceph auth caps replaces every cap, so the existing ones are repeated):"
	echo
	echo "    $grant"
	echo
fi

chmod 0600 "$OUT"/*.keyring
chmod 0644 "$OUT/ceph.conf"
chmod 0755 "$OUT" "$(dirname "$OUT")"

# Hand the tree back to the deploy user, because this script needs root (the
# ceph admin keyring) and ansible does not.
#
# The role copies these keyrings to the nodes with `copy`, whose src is read on
# the CONTROLLER — as the user running kolla-ansible, not as root. Left
# root-owned they are unreadable there and every deploy fails with "Could not
# find or access". The same directory also holds the application credential id,
# which the role WRITES as that user.
#
# The keyrings stay 0600: owned by the deploy user, invisible to everyone else.
if [ -n "${SUDO_USER:-}" ]; then
	chown -R "$SUDO_USER" "$OUT" "$(dirname "$OUT")"
	echo "  owner: $SUDO_USER (the role reads these as the deploy user)"
else
	echo "  NOTE: run this with sudo, not as root, or chown $OUT and its parent"
	echo "        to the user that runs kolla-ansible — ansible reads them as"
	echo "        that user and cannot read a root-only file."
fi

echo
echo "wrote to $OUT:"
ls -l "$OUT"
echo
echo "If this is not the kolla deploy host, copy that directory to"
echo "/etc/kolla/config/ferry/ceph/ there. The role reads it from the deploy"
echo "host and distributes each key only to the component entitled to it."
