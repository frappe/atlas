#!/usr/bin/env bash

set -eu

: "${DATUM_URL:?DATUM_URL is required}"
: "${DATUM_TOKEN:?DATUM_TOKEN is required}"

vector_version=0.59.0
vector_amd64_sha256=1dd9f52bda25d2364ab04d496a3b7edbc775b1a3cd02d9985eb81f3c174a245f
vector_arm64_sha256=5779afdc9abf08373c09b6656c933c8e1da34926c9b646cd80e76d96d7d64133
config_file=/var/lib/metal/metald.toml

if [ "$(id -u)" -ne 0 ]; then
	echo "install-telemetry must run as root" >&2
	exit 1
fi

if [ "$(vector --version 2>/dev/null | cut -d' ' -f2)" != "$vector_version" ]; then
	echo "==> install Vector $vector_version"
	architecture=$(dpkg --print-architecture)
	case "$architecture" in
		amd64) expected_sha256=$vector_amd64_sha256 ;;
		arm64) expected_sha256=$vector_arm64_sha256 ;;
		*) echo "Vector has no package for $architecture" >&2; exit 1 ;;
	esac
	package=$(mktemp --suffix=.deb)
	trap 'rm -f "$package"' EXIT
	curl -fsSL -o "$package" "https://github.com/vectordotdev/vector/releases/download/v$vector_version/vector_$vector_version-1_$architecture.deb"
	echo "$expected_sha256  $package" | sha256sum -c --quiet
	apt-get install -y -qq "$package"
fi
usermod -aG systemd-journal vector

if ! command -v smartctl >/dev/null; then
	echo "==> install smartmontools"
	DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends smartmontools
fi

# --syslog enables RAID monitoring without a mail address.
if command -v mdadm >/dev/null && grep -q '^md' /proc/mdstat; then
	install -d /etc/systemd/system/mdmonitor.service.d /etc/systemd/system/mdmonitor-oneshot.service.d
	printf '[Service]\nExecStart=\nExecStart=/usr/sbin/mdadm --monitor --scan --syslog\n' > /etc/systemd/system/mdmonitor.service.d/syslog.conf
	printf '[Service]\nExecStart=\nExecStart=/usr/sbin/mdadm --monitor --oneshot --scan --syslog\n' > /etc/systemd/system/mdmonitor-oneshot.service.d/syslog.conf
	systemctl daemon-reload
	systemctl reset-failed mdmonitor.service mdmonitor-oneshot.service
	systemctl restart mdmonitor.service
fi

# install-metald.sh edits the same file under the same lock.
if [ ! -f "$config_file" ]; then
	echo "error: $config_file is missing; install metald first" >&2
	exit 1
fi
exec 9>"$config_file.lock"
flock 9
if ! grep -q '^\[telemetry\]' "$config_file"; then
	printf '\n[telemetry]\n' >> "$config_file"
fi
sed -i -e '/^datum_url[[:space:]]*=/d' -e '/^datum_token[[:space:]]*=/d' "$config_file"
sed -i "/^\[telemetry\]/a datum_url = \"${DATUM_URL%/}\"\ndatum_token = \"$DATUM_TOKEN\"" "$config_file"
flock -u 9

# The configuration holds the token, so only root and Vector can read it.
umask 027
{
cat <<'VECTOR'
data_dir: /var/lib/vector

sources:
  # Skip old entries on first start; subsequent starts use the checkpoint.
  metald:
    type: journald
    include_units: [metal.service]
    since_now: true
  host:
    type: journald
    exclude_units: [metal.service]
    since_now: true
    include_matches:
      PRIORITY: ["0", "1", "2", "3", "4"]

transforms:
  datum_lines:
    type: remap
    inputs: [metald, host]
    source: |
      levels = ["emergency", "alert", "critical", "error", "warn", "notice", "info", "debug"]
      line = {
        "ts": format_timestamp!(.timestamp, "%Y-%m-%dT%H:%M:%S%.3fZ"),
        "product": "metal",
        "source": "journal",
        "service": "metald",
        "level": "info",
        "message": to_string(.message) ?? "",
        "attributes": {},
      }
      structured, error = parse_json(to_string(.message) ?? "")
      if error == null && is_object(structured) {
        record = object!(structured)
        line.level = downcase(to_string(record.level) ?? "info")
        line.message = to_string(record.msg) ?? ""
        del(record.time)
        del(record.level)
        del(record.msg)
        line.attributes = map_values(record) -> |value| { to_string(value) ?? encode_json(value) }
      } else {
        priority = to_int(.PRIORITY) ?? 6
        line.level = string(get(levels, [priority]) ?? "info") ?? "info"
        # Go writes a panic or a fatal runtime error as plain text on stderr.
        if match(line.message, r'^(panic|fatal error): ') {
          line.level = "critical"
        }
        if ._TRANSPORT == "kernel" {
          line.service = "kernel"
        } else {
          unit = to_string(._SYSTEMD_UNIT) ?? to_string(.SYSLOG_IDENTIFIER) ?? "host"
          line.service = replace(unit, r'[^a-zA-Z0-9_./-]', "_")
        }
      }
      line.message = truncate(to_string(line.message), limit: 8000)
      . = line

VECTOR
cat <<VECTOR
sinks:
  datum:
    type: http
    inputs: [datum_lines]
    uri: "${DATUM_URL%/}/v1/logs/ingest"
    method: post
    encoding:
      codec: json
    # The JSON codec sends a batch as an array. Datum takes one {"lines": [...]}
    # object per request and 12 requests a minute.
    payload_prefix: '{"lines":'
    payload_suffix: "}"
    batch:
      max_events: 5000
      timeout_secs: 10
    request:
      concurrency: 1
      rate_limit_num: 10
      rate_limit_duration_secs: 60
      headers:
        Authorization: "Bearer $DATUM_TOKEN"
    buffer:
      type: disk
      max_size: 268435488
      when_full: drop_newest
VECTOR
} > /etc/vector/vector.next.yaml
chgrp vector /etc/vector/vector.next.yaml
vector validate --no-environment /etc/vector/vector.next.yaml
mv /etc/vector/vector.next.yaml /etc/vector/vector.yaml

# Vector reads the token once at start, so a new token needs a restart.
systemctl enable --quiet vector
systemctl restart vector
systemctl is-active --quiet vector
echo "==> telemetry configured for ${DATUM_URL%/}"
