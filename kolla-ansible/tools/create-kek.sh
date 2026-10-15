#!/usr/bin/env bash
# Provisions the KEK that wraps every per-domain DEK in Ferry's repositories,
# as a symmetric secret in this cloud's Barbican.
#
# This is deliberately a separate, explicit step rather than something the
# ansible role does. Two reasons, and both have teeth:
#
#   A KEK reminted because a variable was empty is a repository nobody can
#   read. DEKs are only ever stored wrapped, so the KEK is not a credential
#   that can be rotated by replacing it — losing the bytes loses every backup
#   point the repository holds. Barbican secrets are immutable, so a new KEK
#   version is always a new secret.
#
#   Creating the key that decrypts every backup a tenant owns should leave a
#   trail. Barbican gives one; a file written by a deploy does not.
#
# Idempotent: an existing secret with this name is reported and reused rather
# than replaced.
#
# Run it from the kolla deploy host, with admin credentials sourced.
#
#   source /etc/kolla/admin-openrc.sh
#   ./create-kek.sh
#
# Barbican secrets are PROJECT-SCOPED, and Ferry does not read this one as
# admin: ferry-manager authenticates with its own application credential, as the
# `ferry` user in the `service` project. A KEK created under admin and left
# alone gives every repository registration
#
#   403 Forbidden - Secret payload retrieval attempt not allowed
#
# reported as an internal error, because sealing the repository credential is
# the first thing that needs the key. So this grants the ferry user read access
# on the secret it just created. Doing it here rather than telling the operator
# to remember is deliberate — the failure surfaces far from its cause.
#
# Then put the id it prints into /etc/kolla/globals.yml:
#
#   ferry_kms_barbican_secret_id: "<id>"
set -euo pipefail

NAME=${FERRY_KEK_NAME:-ferry-kek}

command -v openstack >/dev/null || {
	echo "openstack client not found; source the kolla venv first" >&2
	exit 1
}
: "${OS_AUTH_URL:?source an openrc first}"

grant_read() { # grant_read <secret href>
	local href=$1 uid
	uid=$(openstack user show "${FERRY_KEYSTONE_USER:-ferry}" -f value -c id 2>/dev/null || true)
	if [ -z "$uid" ]; then
		echo "  WARNING: no keystone user ${FERRY_KEYSTONE_USER:-ferry} yet, so no ACL was set."
		echo "  Run this again after kolla-ansible has created it, or ferry-manager"
		echo "  will get 403 Forbidden reading this secret."
		return
	fi
	# The openstack CLI's `secret acl submit` is not present in every client
	# bundle, so this goes straight at the API.
	local tok
	tok=$(openstack token issue -f value -c id 2>/dev/null) || return
	curl -sS -m 20 -X PUT "$href/acl" \
		-H "X-Auth-Token: $tok" -H "Content-Type: application/json" \
		-d "{\"read\": {\"users\": [\"$uid\"], \"project-access\": true}}" >/dev/null \
		&& echo "  read access granted to ${FERRY_KEYSTONE_USER:-ferry} ($uid)"
}

existing=$(openstack secret list --name "$NAME" -f value -c "Secret href" 2>/dev/null | head -n1 || true)
if [ -n "$existing" ]; then
	echo "a secret named $NAME already exists; reusing it"
	grant_read "$existing"
	echo
	echo "ferry_kms_barbican_secret_id: \"${existing##*/}\""
	exit 0
fi

# 32 bytes, base64. The manager accepts base64 of a 32-byte key, and Barbican
# stores the payload verbatim.
kek=$(head -c 32 /dev/urandom | base64)

href=$(openstack secret store \
	--name "$NAME" \
	--algorithm aes \
	--bit-length 256 \
	--mode cbc \
	--secret-type symmetric \
	--payload-content-type application/octet-stream \
	--payload-content-encoding base64 \
	--payload "$kek" \
	-f value -c "Secret href")


[ -n "$href" ] || {
	echo "barbican did not return a secret href" >&2
	exit 1
}

grant_read "$href"
echo "stored a 32-byte KEK in Barbican as $NAME"
echo
echo "put this in /etc/kolla/globals.yml:"
echo
echo "  ferry_kms_barbican_secret_id: \"${href##*/}\""
echo
echo "Do not copy this key around by hand. To make a repository readable from"
echo "another cloud, export the DOMAIN key rather than the KEK — it travels"
echo "wrapped under a passphrase, with the repository and domain bound in:"
echo "  ferry admin key export --repository <id> --domain <domain>"
echo "  ferry admin key fingerprint --repository <id>   # on the far side, first"
