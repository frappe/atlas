#!/usr/bin/env bash
# Creates a VM via the metald API and SSHes into it with the dev key.
# Run (as root) while `metald serve` is running in another terminal:
#   sudo test/integration/ssh-test.sh
set -euo pipefail

WORKDIR=${METALD_WORKDIR:-/tmp/metald}
ADDR=${METALD_ADDR:-127.0.0.1:8080}
KEY=${METALD_KEY:-$WORKDIR/keys/id_ed25519}
USER=${METALD_SSH_USER:-root}
IMAGE_URL=${METALD_IMAGE_URL:?set METALD_IMAGE_URL}
IMAGE_SHA256=${METALD_IMAGE_SHA256:?set METALD_IMAGE_SHA256}
KERNEL_URL=${METALD_KERNEL_URL:?set METALD_KERNEL_URL}
KERNEL_SHA256=${METALD_KERNEL_SHA256:?set METALD_KERNEL_SHA256}
ARCHITECTURE=${METALD_ARCHITECTURE:-amd64}

api() { curl -sS "http://$ADDR$1" "${@:2}"; }

pubkey=$(cat "$KEY.pub")
requested_id="integration-$(cat /proc/sys/kernel/random/uuid)"
body=$(jq -n \
	--arg image_url "$IMAGE_URL" --arg image_sha256 "$IMAGE_SHA256" \
	--arg kernel_url "$KERNEL_URL" --arg kernel_sha256 "$KERNEL_SHA256" \
	--arg architecture "$ARCHITECTURE" --arg ssh_key "$pubkey" \
	'{vcpus:1,memory_mib:256,disk_mib:1024,image:{ref:"ubuntu",architecture:$architecture,rootfs:{url:$image_url,sha256:$image_sha256},kernel:{url:$kernel_url,sha256:$kernel_sha256}},network:{wireguard_mesh_ipv6:"fdaa::2",egress:"host"},ssh_keys:[$ssh_key]}')
resp=$(api "/vms/$requested_id" -X PUT -H 'content-type: application/json' -d "$body")
id=$(echo "$resp" | jq -r '.id // empty')
if [[ -z $id ]]; then
	echo "create failed: $resp" >&2
	exit 1
fi
echo "created VM $id"
trap 'api "/vms/$id/actions/terminate" -X POST >/dev/null 2>&1 || true' EXIT

echo "waiting for ssh..."
for _ in $(seq 1 30); do
	if ip netns exec "metal-$id" ssh -i "$KEY" \
		-o StrictHostKeyChecking=no -o ConnectTimeout=3 \
		"$USER@172.16.0.2" 'echo metal-ok' 2>/dev/null | grep -q metal-ok; then
		echo "SSH OK - VM $id is reachable"
		exit 0
	fi
	sleep 2
done
echo "FAILED: could not ssh into VM $id" >&2
exit 1
