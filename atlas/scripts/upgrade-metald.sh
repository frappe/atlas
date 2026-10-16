#!/usr/bin/env bash
# Replace metald and atlas-wg-mesh on a provisioned host and set its coordination address.

set -eu
# A failed command inside $(...) also stops the script.
shopt -s inherit_errexit

: "${METALD_DOWNLOAD_URL:?METALD_DOWNLOAD_URL is required}"
: "${METALD_SHA256:?METALD_SHA256 is required}"
: "${WG_MESH_DOWNLOAD_URL:?WG_MESH_DOWNLOAD_URL is required}"
: "${WG_MESH_SHA256:?WG_MESH_SHA256 is required}"
: "${COORDINATION_LISTEN_ADDRESS:?COORDINATION_LISTEN_ADDRESS is required}"
: "${CPU_TEMPLATE_DISABLED:?CPU_TEMPLATE_DISABLED is required}"

metald_binary=${METALD_BINARY_PATH:-/usr/bin/metald}
mesh_binary=${MESH_BINARY_PATH:-/usr/local/bin/atlas-wg-mesh}
service=metal.service
ready_timeout_seconds=120
patches_file=/var/lib/metal/patches.json
config_file=/var/lib/metal/metald.toml
coordination_line="coordination_listen = \"$COORDINATION_LISTEN_ADDRESS\""
case "$CPU_TEMPLATE_DISABLED" in
true) cpu_template_line="cpu_template_disabled = true" ;;
false) cpu_template_line="" ;;
*)
	echo "CPU_TEMPLATE_DISABLED must be true or false" >&2
	exit 1
	;;
esac

if [ "$(id -u)" -ne 0 ]; then
	echo "upgrade-metald must run as root" >&2
	exit 1
fi

# One metald install or upgrade at a time on a host, whichever path starts it.
exec 9>/run/lock/metald-install.lock
if ! flock -n 9; then
	echo "another metald install or upgrade runs on this host" >&2
	exit 1
fi

for binary in "$metald_binary" "$mesh_binary"; do
	if [ ! -x "$binary" ]; then
		echo "$binary is not installed; run install-metald.sh first" >&2
		exit 1
	fi
done

step() { echo "==> $*"; }

# reported_version returns a tool version or "unknown".
reported_version() {
	local tool_version
	tool_version=$("$1" version 2>/dev/null | head -1 | sed 's/^CLI version: //') || tool_version=""
	[ -n "$tool_version" ] || tool_version=unknown
	echo "$tool_version"
}

# cpu_template_config_line returns the CPU template line of the config, or nothing.
cpu_template_config_line() {
	grep '^cpu_template_disabled[[:space:]]*=' "$config_file" || true
}

# set_cpu_template_line replaces the CPU template line. An empty argument removes it.
set_cpu_template_line() {
	sed -i '/^cpu_template_disabled[[:space:]]*=/d' "$config_file"
	if [ -n "$1" ]; then
		sed -i "/^\[firecracker\]/a $1" "$config_file"
	fi
}

# unit_property returns a systemd service property.
unit_property() {
	systemctl show "$service" -p "$1" --value 2>/dev/null || echo ""
}

# A restart stops the transient warm image build units, so only tenant VMs count.
active_virtual_machine_count() {
	systemctl list-units "metal-vm@*" --state=active --no-legend --plain | awk '$1 !~ /^metal-vm@warm-/' | wc -l
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

# service_is_ready waits until metald reports "ready <version>" after its startup.
# A unit without NotifyAccess=main cannot report a status, so only stability counts.
service_is_ready() {
	local expected_status="ready $1" waited_seconds=0

	if [ "$(unit_property NotifyAccess)" != "main" ]; then
		echo "    warning: $service has no NotifyAccess=main; checking only that it stays active"
		service_is_stable
		return
	fi

	while [ "$waited_seconds" -lt "$ready_timeout_seconds" ]; do
		if [ "$(unit_property StatusText)" = "$expected_status" ] && systemctl is-active --quiet "$service"; then
			return 0
		fi
		sleep 1
		waited_seconds=$((waited_seconds + 1))
	done

	echo "    $service did not report \"$expected_status\" within $ready_timeout_seconds seconds" >&2
	return 1
}

# stage downloads one binary next to its destination and checks it.
stage() {
	local url=$1 expected_hash=$2 destination=$3 staged staged_hash

	staged=$(mktemp "$destination.staged.XXXXXX")
	if ! curl -fsSL -o "$staged" "$url"; then
		rm -f "$staged"
		exit 1
	fi

	# The version command runs this file, so check it before that.
	staged_hash=$(sha256sum "$staged" | cut -d' ' -f1)
	if [ "$staged_hash" != "$expected_hash" ]; then
		rm -f "$staged"
		echo "the file at $url has hash $staged_hash, expected $expected_hash" >&2
		exit 1
	fi
	chmod 0755 "$staged"

	if [ "$(reported_version "$staged")" = unknown ]; then
		rm -f "$staged"
		echo "the file at $url has no version command" >&2
		exit 1
	fi
	echo "$staged"
}

# restore_previous puts back the binaries that this upgrade replaced. The previous build cannot read state that a
# new patch changed, even in part, so the new build stays and the fix is a new build.
restore_previous() {
	if [ -n "$new_patches" ]; then
		echo "    $new_version has patches that $current_version cannot undo ($new_patches); keeping $new_version" >&2
		return
	fi
	mv -f "$metald_binary.previous" "$metald_binary"
	mv -f "$mesh_binary.previous" "$mesh_binary"
	# Restore only these lines. install-telemetry.sh can change the rest of the file meanwhile.
	exec 8>"$config_file.lock"
	flock 8
	if [ -n "$previous_coordination_line" ]; then
		sed -i "s|^coordination_listen[[:space:]]*=.*|$previous_coordination_line|" "$config_file"
	else
		sed -i '/^coordination_listen[[:space:]]*=/d' "$config_file"
	fi
	set_cpu_template_line "$previous_cpu_template_line"
	exec 8>&-
	systemctl reset-failed "$service" 2>/dev/null || true
	if ! systemctl restart "$service" || ! service_is_stable; then
		echo "    $service did not start with the previous build" >&2
	fi
}

step "download metald and atlas-wg-mesh"
staged_metald=""
staged_mesh=""
trap 'rm -f "$staged_metald" "$staged_mesh"' EXIT
staged_metald=$(stage "$METALD_DOWNLOAD_URL" "$METALD_SHA256" "$metald_binary")
staged_mesh=$(stage "$WG_MESH_DOWNLOAD_URL" "$WG_MESH_SHA256" "$mesh_binary")

new_version=$(reported_version "$staged_metald")
current_version=$(reported_version "$metald_binary")

# A repeated upgrade after a success changes nothing.
if cmp -s "$staged_metald" "$metald_binary" && cmp -s "$staged_mesh" "$mesh_binary" &&
	grep -qxF "$coordination_line" "$config_file" &&
	[ "$(cpu_template_config_line)" = "$cpu_template_line" ] &&
	[ "$(unit_property StatusText)" = "ready $new_version" ]; then
	step "metald $new_version and atlas-wg-mesh are already installed and running"
	exit 0
fi

step "upgrade metald from $current_version to $new_version"

# A restart keeps the console descriptors that systemd holds, so the virtual
# machines stay up. An empty store means that this metald never stored them.
stored_descriptor_count=$(unit_property NFileDescriptorStore)
if [ "${stored_descriptor_count:-0}" = "0" ]; then
	echo "    systemd holds no descriptor; running virtual machines stop during the restart"
else
	echo "    systemd holds $stored_descriptor_count descriptors; the virtual machines stay up"
fi
virtual_machine_count=$(active_virtual_machine_count)

# A build from before metald patches has no patches command and no patch.
new_patches=""
for patch in $("$staged_metald" patches 2>/dev/null || true); do
	grep -qF "\"$patch\"" "$patches_file" 2>/dev/null || new_patches="$new_patches $patch"
done
new_patches=${new_patches# }

# Keep the running binaries before anything changes, so every later step can undo.
step "back up the running binaries"
cp -a "$metald_binary" "$metald_binary.previous"
cp -a "$mesh_binary" "$mesh_binary.previous"

step "set the coordination address to $COORDINATION_LISTEN_ADDRESS"
# install-telemetry.sh edits the same file under this lock.
exec 8>"$config_file.lock"
flock 8
previous_coordination_line=$(grep '^coordination_listen[[:space:]]*=' "$config_file" || true)
if [ -n "$previous_coordination_line" ]; then
	sed -i "s|^coordination_listen[[:space:]]*=.*|$coordination_line|" "$config_file"
else
	sed -i "/^listen[[:space:]]*=/a $coordination_line" "$config_file"
fi
previous_cpu_template_line=$(cpu_template_config_line)
set_cpu_template_line "$cpu_template_line"
exec 8>&-

# Rename instead of overwrite. The running process keeps its own inode.
step "install the new binaries"
mv -f "$staged_mesh" "$mesh_binary"
mv -f "$staged_metald" "$metald_binary"

# Restart rather than stop and start. systemd keeps its descriptor store across a
# restart even when the unit does not set FileDescriptorStorePreserve=yes.
step "restart $service"
if ! systemctl restart "$service" || ! service_is_ready "$new_version"; then
	echo "    restoring $current_version" >&2
	journalctl -u "$service" -n 20 --no-pager -o cat >&2 || true
	restore_previous
	exit 1
fi

remaining_virtual_machine_count=$(active_virtual_machine_count)
if [ "$remaining_virtual_machine_count" -lt "$virtual_machine_count" ]; then
	echo "    $virtual_machine_count virtual machines ran before the restart and $remaining_virtual_machine_count run now; restoring $current_version" >&2
	restore_previous
	exit 1
fi

step "running metald $new_version and atlas-wg-mesh $(reported_version "$mesh_binary")"
echo "    $remaining_virtual_machine_count virtual machines run"

if [ "$(unit_property FileDescriptorStorePreserve)" != "yes" ]; then
	echo "    warning: $service has no FileDescriptorStorePreserve=yes"
	echo "    a stop of $service still stops every virtual machine"
	echo "    run the Metal Server action Re-configure Metald to update the unit"
fi
