#!/usr/bin/env bash

set -eu -o pipefail

script_directory=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
upgrade_script=$script_directory/../upgrade-metald.sh
test_root=$(mktemp -d)
fixtures=$test_root/fixtures
original_path=$PATH

cleanup() {
	rm -rf "$test_root"
}

trap cleanup EXIT
mkdir -p "$fixtures"

cat > "$fixtures/metald-old" <<'FIXTURE'
#!/usr/bin/env bash
# metald-old
[ "${1:-}" = version ] && echo "metald old"
FIXTURE

cat > "$fixtures/metald-new" <<'FIXTURE'
#!/usr/bin/env bash
# metald-new
[ "${1:-}" = version ] && echo "metald new"
FIXTURE

printf 'wg-mesh-must-not-change\n' > "$fixtures/atlas-wg-mesh"
chmod 0755 "$fixtures"/*
new_metald_hash=$(sha256sum "$fixtures/metald-new" | awk '{ print $1 }')

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

assert_not_contains() {
	local text=$1
	local unexpected=$2

	if [[ "$text" == *"$unexpected"* ]]; then
		echo "expected output not to contain: $unexpected" >&2
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

assert_mesh_unchanged() {
	local current_hash

	current_hash=$(sha256sum "$MESH_SENTINEL" | awk '{ print $1 }')
	if [ "$current_hash" != "$mesh_hash_before" ]; then
		echo "Upgrade Metald changed the WG Mesh sentinel" >&2
		exit 1
	fi
	assert_file_missing "$MESH_SENTINEL.previous"
}

setup_case() {
	case_directory=$(mktemp -d "$test_root/case.XXXXXX")
	fake_bin=$case_directory/bin
	installed_directory=$case_directory/installed
	mkdir -p "$fake_bin" "$installed_directory"

	METALD_BINARY_PATH=$installed_directory/metald
	MESH_SENTINEL=$installed_directory/atlas-wg-mesh
	FAKE_SYSTEMCTL_LOG=$case_directory/systemctl.log
	FAKE_SERVICE_STATE=$case_directory/service-state
	FAKE_DESCRIPTOR_COUNT=$case_directory/descriptor-count
	FAKE_PRESERVE_SETTING=$case_directory/preserve-setting
	FAKE_ACTIVE_UNITS=$case_directory/active-units
	FAKE_LIST_UNITS_CALLS=$case_directory/list-units-calls

	cp "$fixtures/metald-old" "$METALD_BINARY_PATH"
	cp "$fixtures/atlas-wg-mesh" "$MESH_SENTINEL"
	chmod 0755 "$METALD_BINARY_PATH" "$MESH_SENTINEL"
	mesh_hash_before=$(sha256sum "$MESH_SENTINEL" | awk '{ print $1 }')
	: > "$FAKE_SYSTEMCTL_LOG"
	printf 'active\n' > "$FAKE_SERVICE_STATE"
	printf '2\n' > "$FAKE_DESCRIPTOR_COUNT"
	printf 'yes\n' > "$FAKE_PRESERVE_SETTING"
	printf '0\n' > "$FAKE_LIST_UNITS_CALLS"
	cat > "$FAKE_ACTIVE_UNITS" <<'UNITS'
metal-vm@vm-2.service loaded active running VM 2
metal-vm@vm-1.service loaded active running VM 1
UNITS

	cat > "$fake_bin/id" <<'FIXTURE'
#!/usr/bin/env bash
[ "${1:-}" = -u ] && echo 0
FIXTURE

	cat > "$fake_bin/curl" <<'FIXTURE'
#!/usr/bin/env bash
destination=
while [ "$#" -gt 0 ]; do
	case "$1" in
		-o)
			destination=$2
			shift 2
			;;
		-*) shift ;;
		*) shift ;;
	esac
done

[ "${FAIL_DOWNLOAD:-0}" -eq 0 ] || exit 22
cp "$FAKE_FIXTURES/metald-new" "$destination"
FIXTURE

	cat > "$fake_bin/systemctl" <<'FIXTURE'
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
		if [ "${CHANGE_UNITS_BEFORE_RESTART:-0}" -eq 1 ] && [ "$calls" -eq 1 ]; then
			sed 's/vm-2/vm-3/' "$FAKE_ACTIVE_UNITS"
		elif [ "${CHANGE_UNITS_AFTER_RESTART:-0}" -eq 1 ] && [ "$calls" -ge 2 ]; then
			sed 's/vm-2/vm-3/' "$FAKE_ACTIVE_UNITS"
		else
			cat "$FAKE_ACTIVE_UNITS"
		fi
		;;
	restart)
		if [ "${FAIL_CANDIDATE_RESTART:-0}" -eq 1 ] &&
			grep -Fq metald-new "$METALD_BINARY_PATH"; then
			printf 'failed\n' > "$FAKE_SERVICE_STATE"
			exit 1
		fi
		printf 'active\n' > "$FAKE_SERVICE_STATE"
		if [ "${CHANGE_DESCRIPTORS_AFTER_RESTART:-0}" -eq 1 ]; then
			printf '1\n' > "$FAKE_DESCRIPTOR_COUNT"
		fi
		;;
	stop)
		printf 'inactive\n' > "$FAKE_SERVICE_STATE"
		;;
	start)
		printf 'active\n' > "$FAKE_SERVICE_STATE"
		;;
	*) exit 1 ;;
esac
FIXTURE

	cat > "$fake_bin/sleep" <<'FIXTURE'
#!/usr/bin/env bash
exit 0
FIXTURE
	chmod 0755 "$fake_bin"/*

	export PATH=$fake_bin:$original_path
	export METALD_BINARY_PATH MESH_SENTINEL
	export FAKE_SYSTEMCTL_LOG FAKE_SERVICE_STATE FAKE_DESCRIPTOR_COUNT
	export FAKE_PRESERVE_SETTING FAKE_ACTIVE_UNITS FAKE_LIST_UNITS_CALLS
	export FAKE_FIXTURES=$fixtures
	export METALD_DOWNLOAD_URL=https://downloads.test/metald
	export METALD_SHA256=$new_metald_hash
	unset FAIL_DOWNLOAD FAIL_CANDIDATE_RESTART
	unset CHANGE_UNITS_BEFORE_RESTART CHANGE_UNITS_AFTER_RESTART
	unset CHANGE_DESCRIPTORS_AFTER_RESTART
	unset WG_MESH_DOWNLOAD_URL WG_MESH_SHA256 WG_MESH_RESET
	unset MESH_BINARY_PATH MESH_UPLINK_INTERFACE WIREGUARD_INTERFACE
}

run_upgrade() {
	set +e
	run_output=$(bash "$upgrade_script" 2>&1)
	run_status=$?
	set -e
}

test_download_failure_does_not_mutate() {
	setup_case
	export FAIL_DOWNLOAD=1
	run_upgrade
	assert_failure
	assert_contains "$run_output" "could not download metald"
	assert_file_contains "$METALD_BINARY_PATH" metald-old
	assert_file_missing "$METALD_BINARY_PATH.previous"
	assert_mesh_unchanged
	[ ! -s "$FAKE_SYSTEMCTL_LOG" ]
}

test_digest_failure_does_not_mutate() {
	setup_case
	export METALD_SHA256=wrong-digest
	run_upgrade
	assert_failure
	assert_contains "$run_output" "expected wrong-digest"
	assert_file_contains "$METALD_BINARY_PATH" metald-old
	assert_file_missing "$METALD_BINARY_PATH.previous"
	assert_mesh_unchanged
	[ ! -s "$FAKE_SYSTEMCTL_LOG" ]
}

test_preservation_guard_prevents_mutation() {
	setup_case
	printf 'no\n' > "$FAKE_PRESERVE_SETTING"
	run_upgrade
	assert_failure
	assert_contains "$run_output" "must set FileDescriptorStorePreserve=yes"
	assert_file_missing "$METALD_BINARY_PATH.previous"
	assert_mesh_unchanged
	assert_command_count 0 "restart metal.service"
}

test_descriptor_guard_prevents_mutation() {
	setup_case
	printf '1\n' > "$FAKE_DESCRIPTOR_COUNT"
	run_upgrade
	assert_failure
	assert_contains "$run_output" "1 console descriptors for 2 active VM units"
	assert_file_missing "$METALD_BINARY_PATH.previous"
	assert_mesh_unchanged
	assert_command_count 0 "restart metal.service"
}

test_pre_restart_guard_catches_a_vm_change() {
	setup_case
	export CHANGE_UNITS_BEFORE_RESTART=1
	run_upgrade
	assert_failure
	assert_contains "$run_output" "active VM unit set changed before restarting metal.service"
	assert_file_contains "$METALD_BINARY_PATH" metald-old
	assert_mesh_unchanged
	assert_command_count 0 "restart metal.service"
}

test_success_replaces_only_metald() {
	setup_case
	run_upgrade
	assert_success
	assert_file_contains "$METALD_BINARY_PATH" metald-new
	assert_file_contains "$METALD_BINARY_PATH.previous" metald-old
	assert_mesh_unchanged
	assert_not_contains "$run_output" "WG Mesh"
	assert_command_count 1 "restart metal.service"
	assert_command_count 0 "stop metal.service"
	assert_command_count 0 "start metal.service"
}

test_service_failure_restores_metald() {
	setup_case
	export FAIL_CANDIDATE_RESTART=1
	run_upgrade
	assert_failure
	assert_contains "$run_output" "restoring metald old"
	assert_file_contains "$METALD_BINARY_PATH" metald-old
	assert_file_contains "$METALD_BINARY_PATH.previous" metald-old
	assert_mesh_unchanged
	assert_command_count 1 "restart metal.service"
	assert_command_count 1 "stop metal.service"
	assert_command_count 1 "start metal.service"
}

test_changed_vm_set_restores_metald() {
	setup_case
	export CHANGE_UNITS_AFTER_RESTART=1
	run_upgrade
	assert_failure
	assert_contains "$run_output" "active VM unit set changed during the upgrade"
	assert_file_contains "$METALD_BINARY_PATH" metald-old
	assert_mesh_unchanged
	assert_command_count 1 "restart metal.service"
	assert_command_count 1 "stop metal.service"
	assert_command_count 1 "start metal.service"
}

test_changed_descriptor_count_restores_metald() {
	setup_case
	export CHANGE_DESCRIPTORS_AFTER_RESTART=1
	run_upgrade
	assert_failure
	assert_contains "$run_output" "stored console descriptor count changed during the upgrade"
	assert_file_contains "$METALD_BINARY_PATH" metald-old
	assert_mesh_unchanged
	assert_command_count 1 "restart metal.service"
	assert_command_count 1 "stop metal.service"
	assert_command_count 1 "start metal.service"
}

tests=(
	test_download_failure_does_not_mutate
	test_digest_failure_does_not_mutate
	test_preservation_guard_prevents_mutation
	test_descriptor_guard_prevents_mutation
	test_pre_restart_guard_catches_a_vm_change
	test_success_replaces_only_metald
	test_service_failure_restores_metald
	test_changed_vm_set_restores_metald
	test_changed_descriptor_count_restores_metald
)

for test_name in "${tests[@]}"; do
	"$test_name"
	echo "ok - $test_name"
done
