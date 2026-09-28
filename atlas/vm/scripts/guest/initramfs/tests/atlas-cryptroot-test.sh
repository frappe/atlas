#!/bin/sh

set -eu

test_directory=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
script="$test_directory/../scripts/local-top/atlas-cryptroot"
temporary_directory=$(mktemp -d)
stub_directory="$temporary_directory/bin"
mkdir -p "$stub_directory"
trap 'rm -r "$temporary_directory"' EXIT HUP INT TERM

fail() {
	printf 'not ok - %s\n' "$1" >&2
	exit 1
}

assert_contains() {
	pattern=$1
	path=$2
	grep -F -- "$pattern" "$path" >/dev/null || fail "$path does not contain: $pattern"
}

assert_not_contains() {
	pattern=$1
	path=$2
	if grep -F -- "$pattern" "$path" >/dev/null; then
		fail "$path contains: $pattern"
	fi
}

cat > "$temporary_directory/functions" <<'EOF'
panic() {
	printf 'PANIC: %s\n' "$*" >&2
	exit 99
}
EOF

cat > "$stub_directory/cryptsetup" <<'EOF'
#!/bin/sh
printf 'cryptsetup' >> "$ATLAS_TEST_LOG"
printf ' %s' "$@" >> "$ATLAS_TEST_LOG"
printf '\n' >> "$ATLAS_TEST_LOG"

case "$1" in
	isLuks) [ "$ATLAS_TEST_LUKS" -eq 1 ] ;;
	open)
		if [ "${ATLAS_TEST_OPEN_STATUS:-0}" = retry_once ]; then
			attempts=$(cat "$ATLAS_TEST_OPEN_ATTEMPTS")
			attempts=$((attempts + 1))
			printf '%s\n' "$attempts" > "$ATLAS_TEST_OPEN_ATTEMPTS"
			[ "$attempts" -gt 1 ] && exit 0
			exit 2
		else
			exit "${ATLAS_TEST_OPEN_STATUS:-0}"
		fi
		;;
	reencrypt)
		case " $* " in
			*' --resume-only '*) exit "${ATLAS_TEST_RESUME_STATUS:-0}" ;;
			*) exit "${ATLAS_TEST_ENCRYPT_STATUS:-0}" ;;
		esac
		;;
esac
EOF

cat > "$stub_directory/blkid" <<'EOF'
#!/bin/sh
printf 'blkid %s\n' "$*" >> "$ATLAS_TEST_LOG"
printf '%s\n' "$ATLAS_TEST_FILE_SYSTEM"
EOF

cat > "$stub_directory/dumpe2fs" <<'EOF'
#!/bin/sh
printf 'dumpe2fs %s\n' "$*" >> "$ATLAS_TEST_LOG"
cat "$ATLAS_TEST_DUMPE_OUTPUT"
exit "${ATLAS_TEST_DUMPE_STATUS:-0}"
EOF

cat > "$stub_directory/blockdev" <<'EOF'
#!/bin/sh
printf 'blockdev %s\n' "$*" >> "$ATLAS_TEST_LOG"
printf '%s\n' "$ATLAS_TEST_DEVICE_BYTES"
exit "${ATLAS_TEST_BLOCKDEV_STATUS:-0}"
EOF

for command in modprobe udevadm; do
	cat > "$stub_directory/$command" <<'EOF'
#!/bin/sh
printf '%s %s\n' "$(basename "$0")" "$*" >> "$ATLAS_TEST_LOG"
EOF
done
chmod +x "$stub_directory"/*

test_count=0

prepare_case() {
	test_count=$((test_count + 1))
	case_directory="$temporary_directory/case-$test_count"
	mkdir -p "$case_directory"
	command_line_file="$case_directory/cmdline"
	parameter_file="$case_directory/param.conf"
	dumpe_output_file="$case_directory/dumpe2fs.out"
	log_file="$case_directory/commands.log"
	stdout_file="$case_directory/stdout"
	stderr_file="$case_directory/stderr"
	open_attempts_file="$case_directory/open-attempts"
	: > "$parameter_file"
	: > "$log_file"
	printf '0\n' > "$open_attempts_file"
	printf '%s\n' 'atlas.disk_encryption=luks2' > "$command_line_file"
	printf '%s\n' 'Block count: 4194304' 'Block size: 1024' > "$dumpe_output_file"
	test_luks=0
	test_file_system=ext4
	test_device_bytes=4362076160
	test_dumpe_status=0
	test_blockdev_status=0
	test_open_status=0
	test_resume_status=0
	test_encrypt_status=0
}

run_case() {
	env \
		PATH="$stub_directory:$PATH" \
		ATLAS_CRYPTROOT_FUNCTIONS="$temporary_directory/functions" \
		ATLAS_CRYPTROOT_ROOT_DEVICE="$case_directory/root-device" \
		ATLAS_CRYPTROOT_COMMAND_LINE_FILE="$command_line_file" \
		ATLAS_CRYPTROOT_PARAMETER_FILE="$parameter_file" \
		ATLAS_TEST_LOG="$log_file" \
		ATLAS_TEST_LUKS="$test_luks" \
		ATLAS_TEST_FILE_SYSTEM="$test_file_system" \
		ATLAS_TEST_DEVICE_BYTES="$test_device_bytes" \
		ATLAS_TEST_DUMPE_OUTPUT="$dumpe_output_file" \
		ATLAS_TEST_DUMPE_STATUS="$test_dumpe_status" \
		ATLAS_TEST_BLOCKDEV_STATUS="$test_blockdev_status" \
		ATLAS_TEST_OPEN_STATUS="$test_open_status" \
		ATLAS_TEST_OPEN_ATTEMPTS="$open_attempts_file" \
		ATLAS_TEST_RESUME_STATUS="$test_resume_status" \
		ATLAS_TEST_ENCRYPT_STATUS="$test_encrypt_status" \
		"$script" > "$stdout_file" 2> "$stderr_file"
}

expect_success() {
	if ! run_case; then
		cat "$stderr_file" >&2
		fail "$1"
	fi
}

expect_panic() {
	expect_panic_result "$1"
	assert_not_contains 'cryptsetup reencrypt --encrypt' "$log_file"
}

expect_panic_result() {
	set +e
	run_case
	status=$?
	set -e
	[ "$status" -eq 99 ] || {
		cat "$stderr_file" >&2
		fail "$1 returned $status instead of 99"
	}
}

prepare_case
: > "$command_line_file"
expect_success 'plain boot failed'
[ ! -s "$log_file" ] || fail 'plain boot ran an initramfs command'
[ ! -s "$parameter_file" ] || fail 'plain boot changed the root parameter'

prepare_case
printf '%s\n' 'atlas.disk_encryption=luks1' > "$command_line_file"
expect_panic 'unsupported encryption mode'
assert_contains 'unsupported disk encryption mode' "$stderr_file"

prepare_case
test_file_system=ext2
expect_success 'plaintext ext2 with 1 KiB blocks failed'
assert_contains '--device-size 4294967296 --reduce-device-size 32m' "$log_file"
assert_contains 'ROOT=/dev/mapper/root' "$parameter_file"

prepare_case
printf '%s\n' 'Block count: 1048576' 'Block size: 4096' > "$dumpe_output_file"
expect_success 'plaintext ext4 with 4 KiB blocks failed'
assert_contains '--device-size 4294967296 --reduce-device-size 32m' "$log_file"

prepare_case
test_file_system=ext3
expect_success 'plaintext ext3 failed'
assert_contains '--device-size 4294967296 --reduce-device-size 32m' "$log_file"

prepare_case
test_file_system=xfs
expect_panic 'unsupported plaintext file system'
assert_contains 'neither LUKS2 nor a supported root file system' "$stderr_file"

prepare_case
printf '%s\n' 'Block count: 4194304' > "$dumpe_output_file"
expect_panic 'missing block size'
assert_contains 'root file system geometry is ambiguous' "$stderr_file"

prepare_case
printf '%s\n' 'Block count: 4194304' 'Block count: 4194304' 'Block size: 1024' > "$dumpe_output_file"
expect_panic 'duplicate block count'
assert_contains 'root file system geometry is ambiguous' "$stderr_file"

prepare_case
printf '%s\n' 'Block count: invalid' 'Block size: 1024' > "$dumpe_output_file"
expect_panic 'nondecimal block count'
assert_contains 'root file system block count is invalid' "$stderr_file"

prepare_case
printf '%s\n' 'Block count: 0' 'Block size: 1024' > "$dumpe_output_file"
expect_panic 'zero block count'
assert_contains 'root file system block count is invalid' "$stderr_file"

prepare_case
printf '%s\n' 'Block count: 4194304' 'Block size: 0' > "$dumpe_output_file"
expect_panic 'zero block size'
assert_contains 'root file system block size is invalid' "$stderr_file"

prepare_case
printf '%s\n' 'Block count: 4194304' 'Block size: 3072' > "$dumpe_output_file"
expect_panic 'non-power-of-two block size'
assert_contains 'root file system block size is invalid' "$stderr_file"

prepare_case
printf '%s\n' 'Block count: 4194304' 'Block size: 131072' > "$dumpe_output_file"
expect_panic 'unsupported block size'
assert_contains 'root file system block size is invalid' "$stderr_file"

prepare_case
printf '%s\n' 'Block count: 1000000000000000000' 'Block size: 1024' > "$dumpe_output_file"
expect_panic 'block count overflow'
assert_contains 'root file system block count is invalid' "$stderr_file"

prepare_case
test_dumpe_status=1
expect_panic 'dumpe2fs failure'
assert_contains 'could not read the root file system superblock' "$stderr_file"

prepare_case
test_device_bytes=4328521727
expect_panic 'file system without 32 MiB of free space'
assert_contains 'does not leave space for encryption metadata' "$stderr_file"

prepare_case
test_device_bytes=4000000000
expect_panic 'file system larger than the root disk'
assert_contains 'does not leave space for encryption metadata' "$stderr_file"

prepare_case
test_device_bytes=4328521728
expect_success 'file system with exactly 32 MiB of free space failed'
assert_contains '--device-size 4294967296 --reduce-device-size 32m' "$log_file"

prepare_case
printf '%s\n' 'Block count: 1' 'Block size: 1024' > "$dumpe_output_file"
test_device_bytes=33554432
expect_panic 'disk without encryption reduction space'
assert_contains 'does not have space for encryption metadata' "$stderr_file"

prepare_case
test_device_bytes=invalid
expect_panic 'malformed block device size'
assert_contains 'root disk size is invalid' "$stderr_file"

prepare_case
test_blockdev_status=1
expect_panic 'blockdev failure'
assert_contains 'could not read the root disk size' "$stderr_file"

prepare_case
test_device_bytes=0
expect_panic 'zero block device size'
assert_contains 'root disk size is invalid' "$stderr_file"

prepare_case
test_encrypt_status=1
expect_panic_result 'initial encryption failure'
assert_contains 'initial root disk encryption failed' "$stderr_file"

prepare_case
test_luks=1
test_resume_status=1
expect_success 'existing LUKS2 open and reencryption resume failed'
assert_contains 'cryptsetup open' "$log_file"
assert_contains 'cryptsetup reencrypt --resume-only --active-name root' "$log_file"
assert_not_contains 'blkid ' "$log_file"
assert_not_contains 'dumpe2fs ' "$log_file"
assert_not_contains 'blockdev ' "$log_file"
assert_contains 'ROOT=/dev/mapper/root' "$parameter_file"

prepare_case
test_luks=1
test_open_status=retry_once
expect_success 'wrong passphrase retry failed'
assert_contains 'incorrect passphrase; try again' "$stdout_file"
[ "$(grep -c '^cryptsetup open ' "$log_file")" -eq 2 ] || fail 'wrong passphrase did not retry once'

prepare_case
test_luks=1
test_resume_status=2
expect_panic 'reencryption resume failure'
assert_contains 'root disk reencryption could not resume' "$stderr_file"

printf 'ok - %s atlas-cryptroot cases passed\n' "$test_count"
