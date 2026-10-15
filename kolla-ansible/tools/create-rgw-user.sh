#!/usr/bin/env bash
# Mints the RGW gateway user Ferry protects buckets with, and writes it
# where the kolla role expects to find it.
#
# One user, with `system` set, which can read any bucket in the realm — the same
# privilege Ferry already holds over block storage through client.ferry-mover. A
# backup at 02:00 has no caller token to build a trust from, so a per-tenant
# delegation cannot work for the scheduled half of the job; a restore is
# caller-initiated and does use the tenant's own authority.
#
# Run on a host with a Ceph admin keyring — a mon, or wherever you ran
# create-cephx.sh. A kolla control host is not one on any cloud whose Ceph is
# deployed separately, which is why this is a tool and not a role task.
#
# Idempotent: an existing user's keys are read back rather than rotated, so
# re-running it never invalidates a deployed manager.
set -euo pipefail

OUT=${FERRY_OBJECTS_FILE:-/etc/kolla/config/ferry/objects.yml}
UID_=${FERRY_OBJECTS_USER:-ferry-gateway}
NAME=${FERRY_OBJECTS_DISPLAY_NAME:-Ferry backup service}

command -v radosgw-admin >/dev/null || {
	echo "radosgw-admin not found; run this on a host with a Ceph admin keyring" >&2
	exit 1
}
command -v python3 >/dev/null || {
	echo "python3 not found; needed to read radosgw-admin's JSON" >&2
	exit 1
}

echo "== RGW user $UID_ =="
if info=$(radosgw-admin user info --uid="$UID_" 2>/dev/null); then
	echo "  present, keys read back (not rotated)"
else
	echo "  creating"
	info=$(radosgw-admin user create \
		--uid="$UID_" --display-name="$NAME" --system)
fi

# --system is what makes it able to read every tenant's bucket. A user created
# before this flag existed, or created by hand without it, reads nothing and
# fails at the first backup with a 403 — so it is checked rather than assumed.
if ! printf '%s' "$info" | python3 -c '
import json, sys
u = json.load(sys.stdin)
sys.exit(0 if str(u.get("system", "")).lower() in ("true", "1") else 1)
'; then
	echo "  not a system user; setting it"
	info=$(radosgw-admin user modify --uid="$UID_" --system)
fi

# `system` is not admin-ops access, whatever it sounds like. Ferry lists a
# project's buckets through /admin/bucket?uid=…, and without an explicit cap
# RGW answers 403 — which surfaces as a project that simply has no buckets,
# because "no such user" and "you may not ask" are the same empty answer to a
# listing. A production cloud was configured, enabled and blind on exactly this.
#
# buckets=read and nothing else: /admin/bucket is the only admin endpoint Ferry
# calls, and the gateway credential already reads object data over S3.
if ! printf '%s' "$info" | python3 -c '
import json, sys
caps = json.load(sys.stdin).get("caps") or []
ok = any(c.get("type") == "buckets" and "read" in str(c.get("perm", "")) for c in caps)
sys.exit(0 if ok else 1)
'; then
	echo "  granting buckets=read on the admin ops API"
	info=$(radosgw-admin caps add --uid="$UID_" --caps="buckets=read")
fi

read -r ACCESS SECRET <<<"$(printf '%s' "$info" | python3 -c '
import json, sys
u = json.load(sys.stdin)
k = u.get("keys") or []
if not k:
    sys.exit("no S3 keys on this user; add one with: radosgw-admin key create --uid=%s --key-type=s3" % u.get("user_id"))
print(k[0]["access_key"], k[0]["secret_key"])
')"

mkdir -p "$(dirname "$OUT")"
umask 077
cat >"$OUT" <<YAML
---
# Ferry's RGW gateway credential, written by create-rgw-user.sh.
# Loaded by the ferry kolla role when ferry_objects_enabled is on.
ferry_objects_access_key: "$ACCESS"
ferry_objects_secret_key: "$SECRET"
YAML
chmod 0600 "$OUT"

echo
echo "wrote $OUT (mode 0600)"
echo
echo "Next, on the deploy host:"
echo "  1. copy $OUT there if this is not it, owned by the user who runs"
echo "     kolla-ansible — the role reads it with become disabled, so a"
echo "     root-owned copy fails the deploy with \"Could not find or access\""
echo "  2. set  ferry_objects_enabled: true  in globals.yml"
echo "  3. kolla-ansible deploy ... --tags ferry   (or your ferry deploy wrapper)"
echo
echo "The S3 endpoint defaults to kolla's own RGW: http://<internal vip>:<ceph_rgw_port>."
echo "Set ferry_objects_endpoint if this cloud's gateway is somewhere else."
