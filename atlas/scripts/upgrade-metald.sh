#!/usr/bin/env bash
# Replace the metald and Atlas WG Mesh binaries on a provisioned host.

set -eu -o pipefail

: "${METALD_DOWNLOAD_URL:?METALD_DOWNLOAD_URL is required}"
: "${METALD_SHA256:?METALD_SHA256 is required}"
: "${WG_MESH_DOWNLOAD_URL:?WG_MESH_DOWNLOAD_URL is required}"
: "${WG_MESH_SHA256:?WG_MESH_SHA256 is required}"

installed_metald=${METALD_BINARY_PATH:-/usr/bin/metald}
installed_mesh=${MESH_BINARY_PATH:-/usr/local/bin/atlas-wg-mesh}
previous_metald=$installed_metald.previous
previous_mesh=$installed_mesh.previous
service=metal.service
mesh_reset=${WG_MESH_RESET:-0}
mesh_was_reset=0

case "$mesh_reset" in
	0) ;;
	1)
		: "${MESH_UPLINK_INTERFACE:?MESH_UPLINK_INTERFACE is required when WG_MESH_RESET=1}"
		: "${WIREGUARD_INTERFACE:?WIREGUARD_INTERFACE is required when WG_MESH_RESET=1}"
		;;
	*)
		echo "WG_MESH_RESET must be 0 or 1" >&2
		exit 1
		;;
esac

if [ "$(id -u)" -ne 0 ]; then
	echo "upgrade-metald must run as root" >&2
	exit 1
fi

if [ ! -x "$installed_metald" ]; then
	echo "$installed_metald is not installed; run install-metald.sh first" >&2
	exit 1
fi

if [ ! -x "$installed_mesh" ]; then
	echo "$installed_mesh is not installed; run install-metald.sh first" >&2
	exit 1
fi

step() { echo "==> $*"; }

# reported_version returns the first line from a tool's version command.
reported_version() {
	local tool_version version_output
	version_output=$("$1" version 2>/dev/null) || version_output=""
	IFS= read -r tool_version <<< "$version_output"
	[ -n "$tool_version" ] || return 1
	echo "$tool_version"
}

# unit_property returns a systemd service property.
unit_property() {
	systemctl show "$service" -p "$1" --value 2>/dev/null
}

# active_vm_units returns a stable list of active VM service names.
active_vm_units() {
	systemctl list-units "metal-vm@*.service" --state=active --no-legend --no-pager --plain |
		awk 'NF { print $1 }' | LC_ALL=C sort
}

# service_is_stable checks that the service stays active.
service_is_stable() {
	local checks_remaining=5

	while [ "$checks_remaining" -gt 0 ]; do
		systemctl is-active --quiet "$service" || return 1
		checks_remaining=$((checks_remaining - 1))
		[ "$checks_remaining" -eq 0 ] || sleep 1
	done

	return 0
}

staged_metald=
staged_mesh=

cleanup() {
	[ -z "$staged_metald" ] || rm -f "$staged_metald"
	[ -z "$staged_mesh" ] || rm -f "$staged_mesh"
}

trap cleanup EXIT

# stage_binary downloads and validates a binary without changing the host.
stage_binary() {
	local result_variable=$1
	local destination=$2
	local source_url=$3
	local expected_hash=$4
	local label=$5
	local download download_hash download_version

	download=$(mktemp "$destination.staged.XXXXXX")
	printf -v "$result_variable" '%s' "$download"

	step "download $label"
	if ! curl -fsSL -o "$download" "$source_url"; then
		echo "could not download $label from $source_url" >&2
		exit 1
	fi

	download_hash=$(sha256sum "$download" | awk '{ print $1 }')
	if [ "$download_hash" != "$expected_hash" ]; then
		echo "the file at $source_url has hash $download_hash, expected $expected_hash" >&2
		exit 1
	fi
	chmod 0755 "$download"

	if ! download_version=$(reported_version "$download"); then
		echo "the downloaded $label has no version command" >&2
		exit 1
	fi
	echo "    $download_version"
}

# restore_binaries puts the saved pair back and keeps it available for another recovery attempt.
restore_binaries() {
	local restore_failed=0

	if ! cp -a "$previous_metald" "$installed_metald"; then
		echo "    could not restore $installed_metald" >&2
		restore_failed=1
	fi
	if ! cp -a "$previous_mesh" "$installed_mesh"; then
		echo "    could not restore $installed_mesh" >&2
		restore_failed=1
	fi

	return "$restore_failed"
}

# recover_default restores both binaries without removing live WG Mesh state.
recover_default() {
	systemctl stop "$service" >/dev/null 2>&1 || true
	restore_binaries || true

	if ! systemctl start "$service" || ! service_is_stable; then
		echo "    $service did not start with the previous binaries" >&2
	fi
}

# recover_after_mesh_reset rebuilds only the host mesh. Atlas must restore dynamic state.
recover_after_mesh_reset() {
	local candidate_mesh_installed=$1

	systemctl stop "$service" >/dev/null 2>&1 || true
	if [ "$candidate_mesh_installed" -eq 1 ]; then
		"$installed_mesh" reset --force >/dev/null 2>&1 || true
	fi
	restore_binaries || true

	if ! "$installed_mesh" configure \
		--uplink "$MESH_UPLINK_INTERFACE" \
		--wireguard "$WIREGUARD_INTERFACE"; then
		echo "    the previous Atlas WG Mesh binary could not rebuild the host mesh" >&2
	fi

	if ! systemctl start "$service" || ! service_is_stable; then
		echo "    $service did not start with the previous binaries" >&2
	fi

	echo "    WG Mesh reset removed dynamic mesh state; an Atlas sync is required" >&2
}

# fail_after_stop restores service operation after a failure in the mutation phase.
fail_after_stop() {
	local reason=$1
	local candidate_mesh_installed=$2

	echo "    $reason" >&2
	if [ "$mesh_was_reset" -eq 1 ]; then
		recover_after_mesh_reset "$candidate_mesh_installed"
	else
		recover_default
	fi
	exit 1
}

stage_binary staged_metald "$installed_metald" "$METALD_DOWNLOAD_URL" "$METALD_SHA256" metald
stage_binary staged_mesh "$installed_mesh" "$WG_MESH_DOWNLOAD_URL" "$WG_MESH_SHA256" atlas-wg-mesh

current_metald_version=$(reported_version "$installed_metald")
current_mesh_version=$(reported_version "$installed_mesh")
new_metald_version=$(reported_version "$staged_metald")
new_mesh_version=$(reported_version "$staged_mesh")

step "guard running virtual machines"
if ! systemctl is-active --quiet "$service"; then
	echo "$service must be active before an upgrade" >&2
	exit 1
fi

if [ "$(unit_property FileDescriptorStorePreserve)" != "yes" ]; then
	echo "$service must set FileDescriptorStorePreserve=yes before an upgrade" >&2
	exit 1
fi

active_units=$(active_vm_units)
active_unit_count=$(printf '%s\n' "$active_units" | awk 'NF { count++ } END { print count + 0 }')
stored_console_count=$(unit_property NFileDescriptorStore)
if ! [[ "$stored_console_count" =~ ^[0-9]+$ ]]; then
	echo "$service has a non-numeric NFileDescriptorStore value: $stored_console_count" >&2
	exit 1
fi
if [ "$stored_console_count" -ne "$active_unit_count" ]; then
	echo "$service stores $stored_console_count console descriptors for $active_unit_count active VM units" >&2
	exit 1
fi

step "upgrade metald from $current_metald_version to $new_metald_version"
echo "    upgrade atlas-wg-mesh from $current_mesh_version to $new_mesh_version"
if [ "$mesh_reset" -eq 1 ]; then
	echo "    WG_MESH_RESET=1 removes the current WG Mesh state before installation"
fi

step "back up the installed binary pair"
if ! cp -a "$installed_metald" "$previous_metald"; then
	echo "could not back up $installed_metald" >&2
	exit 1
fi
if ! cp -a "$installed_mesh" "$previous_mesh"; then
	echo "could not back up $installed_mesh" >&2
	exit 1
fi

if ! systemctl is-active --quiet "$service"; then
	echo "$service stopped before the upgrade could start" >&2
	exit 1
fi
if ! pre_stop_units=$(active_vm_units); then
	echo "could not confirm the active VM unit set before stopping $service" >&2
	exit 1
fi
if [ "$pre_stop_units" != "$active_units" ]; then
	echo "the active VM unit set changed before stopping $service" >&2
	exit 1
fi
if [ "$(unit_property FileDescriptorStorePreserve)" != "yes" ]; then
	echo "$service lost FileDescriptorStorePreserve=yes before the upgrade could start" >&2
	exit 1
fi
pre_stop_console_count=$(unit_property NFileDescriptorStore)
if ! [[ "$pre_stop_console_count" =~ ^[0-9]+$ ]] || [ "$pre_stop_console_count" -ne "$active_unit_count" ]; then
	echo "$service no longer stores one console descriptor for each active VM unit" >&2
	exit 1
fi

step "stop $service"
if ! systemctl stop "$service"; then
	fail_after_stop "could not stop $service" 0
fi

if [ "$mesh_reset" -eq 1 ]; then
	step "reset WG Mesh with the previous binary"
	mesh_was_reset=1
	if ! "$installed_mesh" reset --force; then
		fail_after_stop "the previous Atlas WG Mesh binary could not reset the host" 0
	fi
fi

step "install the binary pair"
if ! mv -f "$staged_metald" "$installed_metald"; then
	fail_after_stop "could not install $installed_metald" 0
fi
staged_metald=

if ! mv -f "$staged_mesh" "$installed_mesh"; then
	fail_after_stop "could not install $installed_mesh" 0
fi
staged_mesh=

step "start $service"
if ! systemctl start "$service" || ! service_is_stable; then
	fail_after_stop "$service did not stay active; restoring the previous binary pair" 1
fi

if ! current_units=$(active_vm_units); then
	fail_after_stop "could not read the active VM unit set after the upgrade" 1
fi
if [ "$current_units" != "$active_units" ]; then
	fail_after_stop "the active VM unit set changed during the upgrade" 1
fi

if ! current_console_count=$(unit_property NFileDescriptorStore); then
	fail_after_stop "could not read the stored console descriptor count after the upgrade" 1
fi
if ! [[ "$current_console_count" =~ ^[0-9]+$ ]] || [ "$current_console_count" -ne "$stored_console_count" ]; then
	fail_after_stop "the stored console descriptor count changed during the upgrade" 1
fi

if ! "$installed_mesh" status >/dev/null; then
	fail_after_stop "Atlas WG Mesh did not report current status" 1
fi

mesh_version_output=$("$installed_mesh" version 2>/dev/null) ||
	fail_after_stop "Atlas WG Mesh did not report its BPF versions" 1
embedded_bpf=$(printf '%s\n' "$mesh_version_output" | awk -F': ' '$1 == "Embedded BPF" { print $2; exit }')
installed_bpf=$(printf '%s\n' "$mesh_version_output" | awk -F': ' '$1 == "Installed BPF" { print $2; exit }')
if [ -z "$embedded_bpf" ] || [ "$embedded_bpf" != "$installed_bpf" ]; then
	fail_after_stop "Atlas WG Mesh embedded BPF $embedded_bpf does not match installed BPF $installed_bpf" 1
fi

step "running metald $new_metald_version"
echo "    running atlas-wg-mesh $new_mesh_version"
echo "    active VM units: $active_unit_count"
echo "    stored console descriptors: $current_console_count"
echo "    previous metald binary: $previous_metald"
echo "    previous atlas-wg-mesh binary: $previous_mesh"
