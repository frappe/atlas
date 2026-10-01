#!/usr/bin/env bash
# Replace the metald binary on a provisioned host.

set -eu -o pipefail

: "${METALD_DOWNLOAD_URL:?METALD_DOWNLOAD_URL is required}"
: "${METALD_SHA256:?METALD_SHA256 is required}"

installed_binary=${METALD_BINARY_PATH:-/usr/bin/metald}
previous_binary=$installed_binary.previous
service=metal.service

if [ "$(id -u)" -ne 0 ]; then
	echo "upgrade-metald must run as root" >&2
	exit 1
fi

if [ ! -x "$installed_binary" ]; then
	echo "$installed_binary is not installed; run install-metald.sh first" >&2
	exit 1
fi

step() { echo "==> $*"; }

# reported_version returns the first line from the metald version command.
reported_version() {
	local tool_version version_output
	version_output=$("$1" version 2>/dev/null) || version_output=""
	IFS= read -r tool_version <<< "$version_output"
	[ -n "$tool_version" ] || return 1
	echo "$tool_version"
}

# unit_property returns one systemd service property.
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

staged_binary=

cleanup() {
	[ -z "$staged_binary" ] || rm -f "$staged_binary"
}

trap cleanup EXIT

# restore_previous_binary restores metald and starts the previous service.
restore_previous_binary() {
	systemctl stop "$service" >/dev/null 2>&1 || true

	if ! cp -a "$previous_binary" "$installed_binary"; then
		echo "    could not restore $installed_binary" >&2
		return 1
	fi

	if ! systemctl start "$service" || ! service_is_stable; then
		echo "    $service did not start with the previous metald binary" >&2
		return 1
	fi
}

# fail_after_restart restores service operation after a failed candidate restart.
fail_after_restart() {
	local reason=$1

	echo "    $reason" >&2
	restore_previous_binary || true
	exit 1
}

step "download metald"
staged_binary=$(mktemp "$installed_binary.staged.XXXXXX")
if ! curl -fsSL -o "$staged_binary" "$METALD_DOWNLOAD_URL"; then
	echo "could not download metald from $METALD_DOWNLOAD_URL" >&2
	exit 1
fi

download_hash=$(sha256sum "$staged_binary" | awk '{ print $1 }')
if [ "$download_hash" != "$METALD_SHA256" ]; then
	echo "the file at $METALD_DOWNLOAD_URL has hash $download_hash, expected $METALD_SHA256" >&2
	exit 1
fi
chmod 0755 "$staged_binary"

if ! new_version=$(reported_version "$staged_binary"); then
	echo "the downloaded binary has no version command; it is not a metald build" >&2
	exit 1
fi
if ! current_version=$(reported_version "$installed_binary"); then
	echo "$installed_binary has no version command; run install-metald.sh first" >&2
	exit 1
fi

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

step "upgrade metald from $current_version to $new_version"
step "back up $current_version to $previous_binary"
if ! cp -a "$installed_binary" "$previous_binary"; then
	echo "could not back up $installed_binary" >&2
	exit 1
fi

if ! systemctl is-active --quiet "$service"; then
	echo "$service stopped before the upgrade could start" >&2
	exit 1
fi
if ! pre_restart_units=$(active_vm_units); then
	echo "could not confirm the active VM unit set before restarting $service" >&2
	exit 1
fi
if [ "$pre_restart_units" != "$active_units" ]; then
	echo "the active VM unit set changed before restarting $service" >&2
	exit 1
fi
if [ "$(unit_property FileDescriptorStorePreserve)" != "yes" ]; then
	echo "$service lost FileDescriptorStorePreserve=yes before the upgrade could start" >&2
	exit 1
fi
pre_restart_console_count=$(unit_property NFileDescriptorStore)
if ! [[ "$pre_restart_console_count" =~ ^[0-9]+$ ]] ||
	[ "$pre_restart_console_count" -ne "$active_unit_count" ]; then
	echo "$service no longer stores one console descriptor for each active VM unit" >&2
	exit 1
fi

step "install $installed_binary"
if ! mv -f "$staged_binary" "$installed_binary"; then
	echo "could not install $installed_binary; the running binary is unchanged" >&2
	exit 1
fi
staged_binary=

step "restart $service"
if ! systemctl restart "$service" || ! service_is_stable; then
	fail_after_restart "$service did not stay active; restoring $current_version"
fi

if ! current_units=$(active_vm_units); then
	fail_after_restart "could not read the active VM unit set after the upgrade"
fi
if [ "$current_units" != "$active_units" ]; then
	fail_after_restart "the active VM unit set changed during the upgrade"
fi

if ! current_console_count=$(unit_property NFileDescriptorStore); then
	fail_after_restart "could not read the stored console descriptor count after the upgrade"
fi
if ! [[ "$current_console_count" =~ ^[0-9]+$ ]] ||
	[ "$current_console_count" -ne "$stored_console_count" ]; then
	fail_after_restart "the stored console descriptor count changed during the upgrade"
fi

step "running metald $new_version"
echo "    active VM units: $active_unit_count"
echo "    stored console descriptors: $current_console_count"
echo "    previous binary: $previous_binary"
