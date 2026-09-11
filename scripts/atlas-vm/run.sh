#!/usr/bin/env bash
# Run the Atlas control plane in a local Firecracker VM.
set -euo pipefail

script_directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repository_path=$(cd -- "$script_directory/../.." && pwd)

name="${ATLAS_VM_NAME:-atlas}"
virtual_cpu_count=4
memory_mib=8192
disk_gib=24
host_address=172.16.100.1
vm_address=172.16.100.2
network_mask=255.255.255.0
network_prefix=24
guest_mac=06:00:ac:10:64:02
bench_name=atlas-bench
site_name=atlas.localhost
http_port=8000
admin_password=admin
rebuild=false
provision=true

# The Ubuntu release and its checksums are the ones the Metal guest image uses.
# See atlas/vm/scripts/build_ubuntu_server_image.sh.
release_url="https://cloud-images.ubuntu.com/releases/noble/release-20260518"
rootfs_url="$release_url/ubuntu-24.04-server-cloudimg-amd64.squashfs"
rootfs_sha256="bb4bc95d539df92c96ad0ed34c017363e4a7a62772c6af1dc3553e06ce710b74"
kernel_url="$release_url/unpacked/ubuntu-24.04-server-cloudimg-amd64-vmlinuz-generic"
kernel_sha256="3a33b65c88f98a5563c926d5b163ebe09706e5084ba587a19c1b15bd3e7a82d6"

step() { echo "==> $*"; }
fail() { echo "$*" >&2; exit 1; }

usage() {
	cat <<'USAGE'
Usage: run.sh [command] [options]

Commands:
  up        Build the image when it is missing, boot the VM, and provision it. Default.
  down      Stop the VM and remove its host network.
  ssh       Open a shell in the VM.
  logs      Follow the VM serial console.
  status    Report the VM and its addresses.

Options:
  --name NAME             VM name and work directory (default: atlas)
  --vcpu COUNT            Guest vCPU count (default: 4)
  --memory-mib MIB        Guest memory (default: 8192)
  --disk-gib GIB          Guest disk (default: 24)
  --host-address ADDRESS  Host side of the point to point link (default: 172.16.100.1)
  --vm-address ADDRESS    Guest address (default: 172.16.100.2)
  --bench NAME            Bench name in the VM (default: atlas-bench)
  --site NAME             Site name in the VM (default: atlas.localhost)
  --admin-password VALUE  Site Administrator password (default: admin)
  --rebuild               Build the root file system again
  --no-provision          Boot only, do not install Pilot or create the site
USAGE
}

parse_arguments() {
	command=up
	case "${1:-}" in
		up|down|ssh|logs|status) command=$1; shift ;;
		-h|--help) usage; exit 0 ;;
	esac

	while [[ $# -gt 0 ]]; do
		case "$1" in
			--name) name=$2; shift 2 ;;
			--vcpu) virtual_cpu_count=$2; shift 2 ;;
			--memory-mib) memory_mib=$2; shift 2 ;;
			--disk-gib) disk_gib=$2; shift 2 ;;
			--host-address) host_address=$2; shift 2 ;;
			--vm-address) vm_address=$2; shift 2 ;;
			--bench) bench_name=$2; shift 2 ;;
			--site) site_name=$2; shift 2 ;;
			--admin-password) admin_password=$2; shift 2 ;;
			--rebuild) rebuild=true; shift ;;
			--no-provision) provision=false; shift ;;
			-h|--help) usage; exit 0 ;;
			*) fail "unknown argument: $1" ;;
		esac
	done

	work_path="${ATLAS_VM_HOME:-${XDG_CACHE_HOME:-$HOME/.cache}/atlas-vm}/$name"
	tap_device="tap-$name"
	rootfs_path="$work_path/rootfs.ext4"
	kernel_path="$work_path/vmlinux"
	socket_path="$work_path/firecracker.socket"
	configuration_path="$work_path/firecracker.json"
	console_path="$work_path/console.log"
	pid_path="$work_path/firecracker.pid"
	ssh_key_path="$work_path/ssh-key"
}

require_commands() {
	for command_name in "$@"; do
		command -v "$command_name" >/dev/null || fail "missing command: $command_name"
	done
}

is_running() {
	[[ -f $pid_path ]] && kill -0 "$(cat "$pid_path")" 2>/dev/null
}

ssh_guest() {
	ssh -i "$ssh_key_path" \
		-o StrictHostKeyChecking=no \
		-o UserKnownHostsFile=/dev/null \
		-o LogLevel=ERROR \
		-o BatchMode=yes \
		-o ConnectTimeout=5 \
		"root@$vm_address" "$@"
}

# The image build needs root, because unsquashfs only restores file ownership
# and setuid bits as root, and the guest is unusable without them.
build_rootfs() {
	step "build the root file system"
	sudo "$script_directory/build-rootfs.sh" \
		--output "$rootfs_path" \
		--kernel-output "$kernel_path" \
		--disk-gib "$disk_gib" \
		--address "$vm_address" \
		--prefix "$network_prefix" \
		--gateway "$host_address" \
		--authorized-key "$ssh_key_path.pub" \
		--rootfs-url "$rootfs_url" \
		--rootfs-sha256 "$rootfs_sha256" \
		--kernel-url "$kernel_url" \
		--kernel-sha256 "$kernel_sha256"
	sudo chown "$(id -u):$(id -g)" "$rootfs_path" "$kernel_path"
}

ensure_ssh_key() {
	[[ -f $ssh_key_path ]] && return 0
	step "create the VM Secure Shell key"
	ssh-keygen -q -t ed25519 -N "" -C "atlas-vm-$name" -f "$ssh_key_path"
}

# The Atlas VM is a control plane VM and not a hosted VM. Its TAP device stays
# in the host root network namespace with forwarding and address translation, so
# it reaches the internet and the host reaches it directly. A hosted VM instead
# gets a per tenant namespace, a mesh address, and traffic limits.
setup_network() {
	local egress_device
	egress_device=$(ip route show default | awk '/default/ {print $5; exit}')
	[[ -n $egress_device ]] || fail "no default route on this host"

	if ! ip link show "$tap_device" >/dev/null 2>&1; then
		step "create $tap_device"
		sudo ip tuntap add dev "$tap_device" mode tap user "$(id -un)"
		sudo ip address add "$host_address/$network_prefix" dev "$tap_device"
	fi
	sudo ip link set "$tap_device" up

	sudo sysctl -q -w net.ipv4.ip_forward=1
	add_forwarding_rule nat POSTROUTING -s "$vm_address/32" -o "$egress_device" -j MASQUERADE
	add_forwarding_rule filter FORWARD -i "$tap_device" -o "$egress_device" -j ACCEPT
	add_forwarding_rule filter FORWARD -i "$egress_device" -o "$tap_device" \
		-m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
}

# Insert at the top of the chain, so a host firewall policy below does not drop
# the guest traffic.
add_forwarding_rule() {
	local table=$1 chain=$2
	shift 2
	sudo iptables -t "$table" -C "$chain" "$@" 2>/dev/null && return 0
	sudo iptables -t "$table" -I "$chain" "$@"
}

remove_forwarding_rule() {
	local table=$1 chain=$2
	shift 2
	sudo iptables -t "$table" -D "$chain" "$@" 2>/dev/null || true
}

teardown_network() {
	local egress_device
	egress_device=$(ip route show default | awk '/default/ {print $5; exit}')
	if [[ -n $egress_device ]]; then
		remove_forwarding_rule nat POSTROUTING -s "$vm_address/32" -o "$egress_device" -j MASQUERADE
		remove_forwarding_rule filter FORWARD -i "$tap_device" -o "$egress_device" -j ACCEPT
		remove_forwarding_rule filter FORWARD -i "$egress_device" -o "$tap_device" \
			-m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
	fi
	if ip link show "$tap_device" >/dev/null 2>&1; then
		step "remove $tap_device"
		sudo ip link delete "$tap_device"
	fi
}

write_configuration() {
	cat > "$configuration_path" <<JSON
{
  "boot-source": {
    "kernel_image_path": "$kernel_path",
    "boot_args": "console=ttyS0 reboot=k panic=1 pci=off ip=$vm_address::$host_address:$network_mask::eth0:off"
  },
  "drives": [
    {
      "drive_id": "rootfs",
      "path_on_host": "$rootfs_path",
      "is_root_device": true,
      "is_read_only": false
    }
  ],
  "network-interfaces": [
    {
      "iface_id": "eth0",
      "host_dev_name": "$tap_device",
      "guest_mac": "$guest_mac"
    }
  ],
  "machine-config": {
    "vcpu_count": $virtual_cpu_count,
    "mem_size_mib": $memory_mib
  }
}
JSON
}

start_virtual_machine() {
	rm -f "$socket_path"
	write_configuration

	step "start Firecracker"
	nohup firecracker --api-sock "$socket_path" --config-file "$configuration_path" \
		> "$console_path" 2>&1 < /dev/null &
	echo $! > "$pid_path"

	sleep 1
	is_running || { tail -n 20 "$console_path" >&2; fail "Firecracker stopped, see $console_path"; }
}

wait_for_ssh() {
	step "wait for the guest"
	for _ in $(seq 1 120); do
		if ssh_guest true 2>/dev/null; then
			return 0
		fi
		is_running || { tail -n 20 "$console_path" >&2; fail "the VM stopped, see $console_path"; }
		sleep 2
	done
	fail "the guest did not answer Secure Shell, see $console_path"
}

# Only the files Git tracks reach the VM. The working tree also holds build
# output and local caches that are larger than the repository itself.
copy_repository() {
	step "copy the repository"
	ssh_guest "rm -rf /opt/atlas-source && mkdir -p /opt/atlas-source"
	{
		git -C "$repository_path" ls-files -z --cached --others --exclude-standard
		printf '.git\0'
	} | tar -C "$repository_path" --null --files-from - --ignore-failed-read -cf - \
		| ssh_guest "tar -C /opt/atlas-source --no-same-owner -xf -"
}

provision_guest() {
	local branch
	branch=$(git -C "$repository_path" rev-parse --abbrev-ref HEAD)
	[[ $branch != HEAD ]] || fail "the repository is on a detached HEAD, check out a branch first"

	copy_repository
	step "provision the guest"
	ssh_guest "cat > /root/provision.sh && chmod +x /root/provision.sh" < "$script_directory/provision.sh"
	ssh_guest "/root/provision.sh \
		--bench '$bench_name' \
		--site '$site_name' \
		--source /opt/atlas-source \
		--branch '$branch' \
		--admin-password '$admin_password'"
}

report() {
	cat <<REPORT

Atlas is up.

  Desk       http://$vm_address:$http_port  (Administrator / $admin_password)
  Shell      $script_directory/run.sh ssh --name $name
  Console    $script_directory/run.sh logs --name $name
  Stop       $script_directory/run.sh down --name $name

REPORT
}

command_up() {
	require_commands firecracker ip iptables ssh ssh-keygen tar git sudo
	[[ -e /dev/kvm ]] || fail "/dev/kvm is missing, this host cannot run Firecracker"
	[[ -r /dev/kvm && -w /dev/kvm ]] || fail "/dev/kvm is not readable and writable by $(id -un)"

	mkdir -p "$work_path"
	ensure_ssh_key
	if $rebuild || [[ ! -f $rootfs_path || ! -f $kernel_path ]]; then
		build_rootfs
	fi

	setup_network
	if is_running; then
		step "the VM is already running"
	else
		start_virtual_machine
	fi

	wait_for_ssh
	if $provision; then
		provision_guest
		report
	fi
}

command_down() {
	if is_running; then
		step "stop the VM"
		kill "$(cat "$pid_path")" 2>/dev/null || true
		for _ in $(seq 1 20); do
			is_running || break
			sleep 0.5
		done
		is_running && kill -9 "$(cat "$pid_path")" 2>/dev/null || true
	fi
	rm -f "$pid_path" "$socket_path"
	teardown_network
}

command_status() {
	if is_running; then
		echo "running   pid $(cat "$pid_path")   $vm_address"
	else
		echo "stopped"
	fi
	echo "work      $work_path"
}

main() {
	parse_arguments "$@"
	case "$command" in
		up) command_up ;;
		down) command_down ;;
		ssh) ssh_guest ;;
		logs) tail -f "$console_path" ;;
		status) command_status ;;
	esac
}

main "$@"
