#!/usr/bin/env bash
# Creates a VM via the metald API and SSHes into it with the dev key.
# Run (as root) while `metald up` is running in another terminal:
#   sudo test/integration/ssh-test.sh
set -euo pipefail

WORKDIR=${METALD_WORKDIR:-/tmp/metald}
ADDR=${METALD_ADDR:-127.0.0.1:8080}
KEY=${METALD_KEY:-$WORKDIR/keys/id_ed25519}
USER=${METALD_SSH_USER:-root}

api() { curl -sS "http://$ADDR$1" "${@:2}"; }

pubkey=$(cat "$KEY.pub")
resp=$(api /vms -X POST -H 'content-type: application/json' \
	-d "{\"vcpus\":1,\"mem_mib\":256,\"image\":\"ubuntu\",\"network\":\"default\",\"ssh_keys\":[\"$pubkey\"]}")
id=$(echo "$resp" | jq -r '.id // empty')
if [[ -z $id ]]; then
	echo "create failed: $resp" >&2
	exit 1
fi
echo "created VM $id"
trap 'api "/vms/$id" -X DELETE >/dev/null 2>&1 || true' EXIT

echo "waiting for ssh..."
for _ in $(seq 1 30); do
	if ip netns exec "metal-$id" ssh -i "$KEY" \
		-o StrictHostKeyChecking=no -o ConnectTimeout=3 \
		"$USER@172.16.0.2" 'echo metal-ok' 2>/dev/null | grep -q metal-ok; then
		echo "SSH OK — VM $id is reachable"
		exit 0
	fi
	sleep 2
done
echo "FAILED: could not ssh into VM $id" >&2
exit 1
