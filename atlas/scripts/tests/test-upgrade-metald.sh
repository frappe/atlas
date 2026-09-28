#!/usr/bin/env bash

set -eu -o pipefail

script_directory=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
upgrade_script=$script_directory/../upgrade-metald.sh
test_root=$(mktemp -d)
fixtures=$test_root/fixtures

cleanup() {
	rm -rf "$test_root"
}

trap cleanup EXIT
mkdir -p "$fixtures"

cat > "$fixtures/metald-old" <<'EOF'
#!/usr/bin/env bash
# metald-old
[ "${1:-}" = version ] && echo "metald old"
EOF

cat > "$fixtures/metald-new" <<'EOF'
#!/usr/bin/env bash
# metald-new
[ "${1:-}" = version ] && echo "metald new"
EOF

cat > "$fixtures/mesh-old" <<'EOF'
#!/usr/bin/env bash
# mesh-old
echo "old:$*" >> "$FAKE_MESH_LOG"
case "${1:-}" in
	version)
		echo "CLI version: old"
		echo "Embedded BPF: old-bpf"
		echo "Installed BPF: old-bpf"
		;;
	status | reset | configure) ;;
	*) exit 1 ;;
esac
EOF

cat > "$fixtures/mesh-new" <<'EOF'
#!/usr/bin/env bash
# mesh-new
echo "new:$*" >> "$FAKE_MESH_LOG"
case "${1:-}" in
	version)
		echo "CLI version: new"
		echo "Embedded BPF: new-bpf"
		if [ "${MESH_BPF_MISMATCH:-0}" -eq 1 ]; then
			echo "Installed BPF: other-bpf"
		else
			echo "Installed BPF: new-bpf"
		fi
		;;
	status)
		[ "${FAIL_MESH_STATUS:-0}" -eq 0 ]
		;;
	reset | configure) ;;
	*) exit 1 ;;
esac
EOF

chmod 0755 "$fixtures"/*

new_metald_hash=$(sha256sum "$fixtures/metald-new" | awk '{ print $1 }')
new_mesh_hash=$(sha256sum "$fixtures/mesh-new" | awk '{ print $1 }')

assert_success() {
	if [ "$run_status" -ne 0 ]; then
		echo "expected success, got $run_status" >&2
		echo "$run_output" >&2
		exit 1
	fi
}

assert_failure() {
	if [ "$run_status" -eq 0 ]; then
		echo "expected failure" >&2
		echo "$run_output" >&2
		exit 1
	fi
}

assert_contains() {
	local text=$1
	local expected=$2

	if [[ "$text" != *"$expected"* ]]; then
		echo "expected output to contain: $expected" >&2
		echo "$text" >&2
		exit 1
	fi
}

assert_file_contains() {
	local path=$1
	local expected=$2

	if ! grep -Fq "$expected" "$path"; then
		echo "expected $path to contain: $expected" >&2
		exit 1
	fi
}

assert_file_missing() {
	if [ -e "$1" ]; then
		echo "expected $1 to be absent" >&2
		exit 1
	fi
}

assert_command_count() {
	local expected=$1
	local command=$2
	local actual

	actual=$(grep -Fxc "$command" "$FAKE_SYSTEMCTL_LOG" || true)
	if [ "$actual" -ne "$expected" ]; then
		echo "expected $expected occurrences of '$command', got $actual" >&2
		cat "$FAKE_SYSTEMCTL_LOG" >&2
		exit 1
	fi
}

setup_case() {
	case_directory=$(mktemp -d "$test_root/case.XXXXXX")
	fake_bin=$case_directory/bin
	installed_directory=$case_directory/installed
	mkdir -p "$fake_bin" "$installed_directory"

	METALD_BINARY_PATH=$installed_directory/metald
	MESH_BINARY_PATH=$installed_directory/atlas-wg-mesh
	FAKE_SYSTEMCTL_LOG=$case_directory/systemctl.log
	FAKE_MESH_LOG=$case_directory/mesh.log
	FAKE_SERVICE_STATE=$case_directory/service-state
	FAKE_DESCRIPTOR_COUNT=$case_directory/descriptor-count
	FAKE_PRESERVE_SETTING=$case_directory/preserve-setting
	FAKE_ACTIVE_UNITS=$case_directory/active-units
	FAKE_LIST_UNITS_CALLS=$case_directory/list-units-calls

	cp "$fixtures/metald-old" "$METALD_BINARY_PATH"
	cp "$fixtures/mesh-old" "$MESH_BINARY_PATH"
	chmod 0755 "$METALD_BINARY_PATH" "$MESH_BINARY_PATH"
	: > "$FAKE_SYSTEMCTL_LOG"
	: > "$FAKE_MESH_LOG"
	printf 'active\n' > "$FAKE_SERVICE_STATE"
	printf '2\n' > "$FAKE_DESCRIPTOR_COUNT"
	printf 'yes\n' > "$FAKE_PRESERVE_SETTING"
	printf '0\n' > "$FAKE_LIST_UNITS_CALLS"
	cat > "$FAKE_ACTIVE_UNITS" <<'EOF'
metal-vm@vm-2.service loaded active running VM 2
metal-vm@vm-1.service loaded active running VM 1
EOF

	cat > "$fake_bin/id" <<'EOF'
#!/usr/bin/env bash
[ "${1:-}" = -u ] && echo 0
EOF

	cat > "$fake_bin/curl" <<'EOF'
#!/usr/bin/env bash
destination=
url=
while [ "$#" -gt 0 ]; do
	case "$1" in
		-o)
			destination=$2
			shift 2
			;;
		-*) shift ;;
		*)
			url=$1
			shift
			;;
	esac
done

if [ "${FAIL_DOWNLOAD_URL:-}" = "$url" ]; then
	exit 22
fi

case "$url" in
	https://downloads.test/metald) cp "$FAKE_FIXTURES/metald-new" "$destination" ;;
	https://downloads.test/mesh) cp "$FAKE_FIXTURES/mesh-new" "$destination" ;;
	*) exit 22 ;;
esac
EOF

	cat > "$fake_bin/systemctl" <<'EOF'
#!/usr/bin/env bash
echo "$*" >> "$FAKE_SYSTEMCTL_LOG"
case "${1:-}" in
	is-active)
		[ "$(cat "$FAKE_SERVICE_STATE")" = active ]
		;;
	show)
		property=
		while [ "$#" -gt 0 ]; do
			if [ "$1" = -p ]; then
				property=$2
				break
			fi
			shift
		done
		case "$property" in
			FileDescriptorStorePreserve) cat "$FAKE_PRESERVE_SETTING" ;;
			NFileDescriptorStore) cat "$FAKE_DESCRIPTOR_COUNT" ;;
			*) exit 1 ;;
		esac
		;;
	list-units)
		calls=$(cat "$FAKE_LIST_UNITS_CALLS")
		printf '%s\n' "$((calls + 1))" > "$FAKE_LIST_UNITS_CALLS"
		if [ "${CHANGE_UNITS_BEFORE_STOP:-0}" -eq 1 ] && [ "$calls" -eq 1 ]; then
			sed 's/vm-2/vm-3/' "$FAKE_ACTIVE_UNITS"
		else
			cat "$FAKE_ACTIVE_UNITS"
		fi
		;;
	stop)
		printf 'inactive\n' > "$FAKE_SERVICE_STATE"
		;;
	start)
		if [ "${FAIL_CANDIDATE_START:-0}" -eq 1 ] && grep -Fq metald-new "$METALD_BINARY_PATH"; then
			printf 'failed\n' > "$FAKE_SERVICE_STATE"
			exit 1
		fi
		printf 'active\n' > "$FAKE_SERVICE_STATE"
		;;
	*) exit 1 ;;
esac
EOF

	cat > "$fake_bin/sleep" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

	chmod 0755 "$fake_bin"/*

	export PATH=$fake_bin:$PATH
	export METALD_BINARY_PATH MESH_BINARY_PATH
	export FAKE_SYSTEMCTL_LOG FAKE_MESH_LOG FAKE_SERVICE_STATE FAKE_DESCRIPTOR_COUNT
	export FAKE_PRESERVE_SETTING FAKE_ACTIVE_UNITS FAKE_LIST_UNITS_CALLS
	export FAKE_FIXTURES=$fixtures
	export METALD_DOWNLOAD_URL=https://downloads.test/metald
	export WG_MESH_DOWNLOAD_URL=https://downloads.test/mesh
	export METALD_SHA256=$new_metald_hash
	export WG_MESH_SHA256=$new_mesh_hash
	export WG_MESH_RESET=0
	unset FAIL_DOWNLOAD_URL FAIL_CANDIDATE_START FAIL_MESH_STATUS MESH_BPF_MISMATCH
	unset CHANGE_UNITS_BEFORE_STOP
	unset MESH_UPLINK_INTERFACE WIREGUARD_INTERFACE
}

run_upgrade() {
	set +e
	run_output=$(bash "$upgrade_script" 2>&1)
	run_status=$?
	set -e
}

test_download_failure_does_not_mutate() {
	setup_case
	export FAIL_DOWNLOAD_URL=$METALD_DOWNLOAD_URL
	run_upgrade
	assert_failure
	assert_contains "$run_output" "could not download metald"
	assert_file_contains "$METALD_BINARY_PATH" metald-old
	assert_file_contains "$MESH_BINARY_PATH" mesh-old
	assert_file_missing "$METALD_BINARY_PATH.previous"
	assert_file_missing "$MESH_BINARY_PATH.previous"
	[ ! -s "$FAKE_SYSTEMCTL_LOG" ]
}

test_digest_failure_does_not_mutate() {
	setup_case
	export WG_MESH_SHA256=wrong-digest
	run_upgrade
	assert_failure
	assert_contains "$run_output" "expected wrong-digest"
	assert_file_contains "$METALD_BINARY_PATH" metald-old
	assert_file_contains "$MESH_BINARY_PATH" mesh-old
	assert_file_missing "$METALD_BINARY_PATH.previous"
	assert_file_missing "$MESH_BINARY_PATH.previous"
	[ ! -s "$FAKE_SYSTEMCTL_LOG" ]
}

test_descriptor_guard_prevents_mutation() {
	setup_case
	printf '1\n' > "$FAKE_DESCRIPTOR_COUNT"
	run_upgrade
	assert_failure
	assert_contains "$run_output" "1 console descriptors for 2 active VM units"
	assert_file_missing "$METALD_BINARY_PATH.previous"
	assert_file_missing "$MESH_BINARY_PATH.previous"
	assert_command_count 0 "stop metal.service"
	assert_command_count 0 "start metal.service"
}

test_pre_stop_guard_catches_a_vm_change() {
	setup_case
	export CHANGE_UNITS_BEFORE_STOP=1
	run_upgrade
	assert_failure
	assert_contains "$run_output" "the active VM unit set changed before stopping metal.service"
	assert_file_contains "$METALD_BINARY_PATH" metald-old
	assert_file_contains "$MESH_BINARY_PATH" mesh-old
	assert_command_count 0 "stop metal.service"
	assert_command_count 0 "start metal.service"
}

test_success_replaces_the_binary_pair_once() {
	setup_case
	run_upgrade
	assert_success
	assert_file_contains "$METALD_BINARY_PATH" metald-new
	assert_file_contains "$MESH_BINARY_PATH" mesh-new
	assert_file_contains "$METALD_BINARY_PATH.previous" metald-old
	assert_file_contains "$MESH_BINARY_PATH.previous" mesh-old
	assert_command_count 1 "stop metal.service"
	assert_command_count 1 "start metal.service"
	assert_file_contains "$FAKE_MESH_LOG" "new:status"
	if grep -Fq "reset --force" "$FAKE_MESH_LOG"; then
		echo "default upgrade unexpectedly reset WG Mesh" >&2
		exit 1
	fi
}

test_service_failure_restores_both_without_mesh_reset() {
	setup_case
	export FAIL_CANDIDATE_START=1
	run_upgrade
	assert_failure
	assert_contains "$run_output" "restoring the previous binary pair"
	assert_file_contains "$METALD_BINARY_PATH" metald-old
	assert_file_contains "$MESH_BINARY_PATH" mesh-old
	assert_command_count 2 "stop metal.service"
	assert_command_count 2 "start metal.service"
	if grep -Fq "reset --force" "$FAKE_MESH_LOG"; then
		echo "default rollback unexpectedly reset WG Mesh" >&2
		exit 1
	fi
}

test_bpf_mismatch_restores_both() {
	setup_case
	export MESH_BPF_MISMATCH=1
	run_upgrade
	assert_failure
	assert_contains "$run_output" "embedded BPF new-bpf does not match installed BPF other-bpf"
	assert_file_contains "$METALD_BINARY_PATH" metald-old
	assert_file_contains "$MESH_BINARY_PATH" mesh-old
}

test_explicit_reset_recovers_with_the_old_cli() {
	setup_case
	export WG_MESH_RESET=1
	export MESH_UPLINK_INTERFACE=eno1.1878
	export WIREGUARD_INTERFACE=wg-test
	export FAIL_CANDIDATE_START=1
	run_upgrade
	assert_failure
	assert_contains "$run_output" "an Atlas sync is required"
	assert_file_contains "$METALD_BINARY_PATH" metald-old
	assert_file_contains "$MESH_BINARY_PATH" mesh-old
	assert_file_contains "$FAKE_MESH_LOG" "old:reset --force"
	assert_file_contains "$FAKE_MESH_LOG" "new:reset --force"
	assert_file_contains "$FAKE_MESH_LOG" "old:configure --uplink eno1.1878 --wireguard wg-test"
	assert_command_count 2 "start metal.service"
}

test_invalid_reset_value_is_rejected() {
	setup_case
	export WG_MESH_RESET=yes
	run_upgrade
	assert_failure
	assert_contains "$run_output" "WG_MESH_RESET must be 0 or 1"
	[ ! -s "$FAKE_SYSTEMCTL_LOG" ]
}

tests=(
	test_download_failure_does_not_mutate
	test_digest_failure_does_not_mutate
	test_descriptor_guard_prevents_mutation
	test_pre_stop_guard_catches_a_vm_change
	test_success_replaces_the_binary_pair_once
	test_service_failure_restores_both_without_mesh_reset
	test_bpf_mismatch_restores_both
	test_explicit_reset_recovers_with_the_old_cli
	test_invalid_reset_value_is_rejected
)

for test_name in "${tests[@]}"; do
	"$test_name"
	echo "ok - $test_name"
done
