#!/usr/bin/env bash
# Demonstrate in-place restore and warm VM creation from a memory snapshot.
# Run as root. The script starts metald when it is not already running:
#   sudo -E metal/test/integration/snapshot-demo.sh
set -euo pipefail

MODULE=$(cd "$(dirname "$0")/../.." && pwd)
WORKDIR=${METALD_WORKDIR:-/tmp/metald}
ADDR=${METALD_ADDR:-127.0.0.1:8080}
KEY=${METALD_KEY:-$WORKDIR/keys/id_ed25519}
USER=${METALD_SSH_USER:-root}
POOL=${METALD_POOL:-metal}
CONFIG=$WORKDIR/metald.toml
REF=${METALD_IMAGE_REF:-golden-$$}

[[ $EUID -eq 0 ]] || { echo "run as root: sudo -E $0" >&2; exit 1; }

api() { curl -sS "http://$ADDR$1" "${@:2}"; }
ssh_vm() {
	ip netns exec "metal-$1" ssh -i "$KEY" \
		-o StrictHostKeyChecking=no -o ConnectTimeout=3 "$USER@172.16.0.2" "$2" 2>/dev/null
}
fail() { echo "FAIL: $*" >&2; exit 1; }

wait_api() { for _ in $(seq 1 60); do api /health >/dev/null 2>&1 && return 0; sleep 1; done; return 1; }
wait_state() { for _ in $(seq 1 60); do [[ $(api "/vms/$1" | jq -r '.state') == "$2" ]] && return 0; sleep 1; done; return 1; }
wait_ssh() { for _ in $(seq 1 30); do ssh_vm "$1" 'echo ok' | grep -q ok && return 0; sleep 2; done; return 1; }

create() {
	local resp id
	resp=$(api /vms -X POST -H 'content-type: application/json' \
		-d "{\"vcpus\":1,\"mem_mib\":256,\"image\":\"$1\",\"network\":\"default\",\"ssh_keys\":[\"$PUBKEY\"]}")
	id=$(printf '%s' "$resp" | jq -r '.id // empty')
	[[ -n $id ]] || { echo "create($1) response: $resp" >&2; return 1; }
	printf '%s' "$id"
}

LOG=/tmp/metald-demo.log

# Bootstrap the host when metald is not running and its pool or config is missing.
if ! api /health >/dev/null 2>&1; then
	rm -f "$WORKDIR/metal.sock" 2>/dev/null || true
	# Remove stale network resources so a reused VM ID does not collide with a veth.
	for ns in $(ip netns list 2>/dev/null | awk '/^metal-/{print $1}'); do ip netns del "$ns" 2>/dev/null || true; done
	for vh in $(ip -o link show 2>/dev/null | grep -o 'vh-[0-9]\+' | sort -u); do ip link del "$vh" 2>/dev/null || true; done
	systemctl reset-failed 'metal-vm@*' 2>/dev/null || true
	if ! zpool list "$POOL" >/dev/null 2>&1 || [[ ! -f $CONFIG ]]; then
		echo "==> bootstrapping the development host"
		METALD_LISTEN="$ADDR" METALD_POOL="$POOL" "$MODULE/scripts/dev.sh"
	fi
	echo "==> starting metald (log: $LOG)"
	(cd "$MODULE" && go build -o "$WORKDIR/bin/metald" ./cmd/metald)
	"$WORKDIR/bin/metald" serve --config "$CONFIG" >"$LOG" 2>&1 &
	METALD_PID=$!
	trap 'kill $METALD_PID 2>/dev/null || true' EXIT
	if ! wait_api; then
		echo "--- $LOG (last 30 lines) ---" >&2
		tail -30 "$LOG" >&2
		fail "metald did not come up"
	fi
fi
PUBKEY=$(cat "$KEY.pub")

# Record a disk marker and the guest boot ID.
echo "==> create VM"
id=$(create ubuntu) || fail "create failed (see response above)"
echo "   VM $id"
wait_ssh "$id" || fail "cannot ssh into $id"
ssh_vm "$id" 'echo BEFORE > /root/marker'
bid0=$(ssh_vm "$id" 'cat /proc/sys/kernel/random/boot_id')
echo "   marker=BEFORE boot_id=$bid0"

# Restore the VM and verify that it resumes the captured state.
echo "==> create memory snapshot 'demo' -> change disk -> restore"
api "/vms/$id/snapshots" -X POST -H 'content-type: application/json' -d '{"name":"demo","memory":true}' >/dev/null
ssh_vm "$id" 'echo AFTER > /root/marker'
api "/vms/$id/snapshots/demo/restore" -X POST >/dev/null
wait_state "$id" running || fail "restore did not resume the VM"
wait_ssh "$id" || fail "cannot ssh after restore"
marker=$(ssh_vm "$id" 'cat /root/marker')
bid1=$(ssh_vm "$id" 'cat /proc/sys/kernel/random/boot_id')
echo "   marker=$marker boot_id=$bid1"
[[ $marker == BEFORE ]] || fail "disk was not rolled back (marker=$marker)"
[[ $bid1 == "$bid0" ]] || fail "guest boot ID changed; the VM started from a new boot instead of the captured memory"
echo "   PASS restore in place: disk rolled back, memory resumed"

# Promote the snapshot, remove the source VM, and create a clone.
echo "==> promote memory snapshot 'demo' -> warm image '$REF'"
api "/vms/$id/snapshots/demo/promote" -X POST -H 'content-type: application/json' -d "{\"image\":\"$REF\"}" >/dev/null
api /images | jq --arg r "$REF" -e '.images[] | select(.ref==$r and .warm==true)' >/dev/null || fail "'$REF' not listed as a warm image"
memsum0=$(sha256sum "$IMAGES_DIR/$REF/mem" | cut -d' ' -f1)

echo "==> destroy source VM $id (the image is independent of it)"
api "/vms/$id" -X DELETE >/dev/null

echo "==> create a VM from warm image '$REF'"
cid=$(create "$REF") || fail "warm create failed (see response above)"
echo "   clone $cid"
wait_ssh "$cid" || fail "cannot ssh into the clone $cid"
cmarker=$(ssh_vm "$cid" 'cat /root/marker')
cbid=$(ssh_vm "$cid" 'cat /proc/sys/kernel/random/boot_id')
memsum1=$(sha256sum "$IMAGES_DIR/$REF/mem" | cut -d' ' -f1)
echo "   marker=$cmarker boot_id=$cbid"
[[ $cmarker == BEFORE ]] || fail "clone did not start from the captured disk (marker=$cmarker)"
[[ $cbid == "$bid0" ]] || fail "clone did not start from the captured memory (boot_id=$cbid)"
[[ $memsum0 == "$memsum1" ]] || fail "the warm image memory snapshot changed on resume (not read-only)"
echo "   PASS warm image create: clone resumed the captured memory and disk; image memory unchanged"

# Clean up.
api "/vms/$cid" -X DELETE >/dev/null
api "/images/$REF" -X DELETE >/dev/null || true
echo "ALL PASS"
