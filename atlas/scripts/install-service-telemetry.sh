#!/usr/bin/env bash

set -eu

: "${DATUM_URL:?DATUM_URL is required}"
: "${DATUM_TOKEN:?DATUM_TOKEN is required}"
: "${SERVICE:?SERVICE is required}"

if [ "$(id -u)" -ne 0 ]; then
	echo "install-service-telemetry must run as root" >&2
	exit 1
fi

datum_url=${DATUM_URL%/}
metric_inputs=system
log_inputs=journal_lines

# The configuration holds the token, so only root and Vector can read it.
umask 027
rm -f /etc/vector/vector.next.yaml
{
cat <<'VECTOR'
data_dir: /var/lib/vector

sources:
  system:
    type: host_metrics
    namespace: service
    scrape_interval_secs: 30
    collectors: [cpu, memory, filesystem, load, network, disk]
    filesystem:
      mountpoints:
        excludes: ["/proc", "/proc/*", "/sys", "/sys/*", "/dev", "/dev/*", "/run/credentials/*", "/run/user/*", "/snap/*"]
    network:
      devices:
        excludes: ["lo"]
    disk:
      devices:
        excludes: ["loop*", "ram*"]
  # Skip old entries on first start; subsequent starts use the checkpoint.
  journal:
    type: journald
    since_now: true
    include_matches:
      PRIORITY: ["0", "1", "2", "3", "4"]
VECTOR

if [ "$SERVICE" = "proxy" ]; then
	metric_inputs="system, nginx_metrics"
	log_inputs="journal_lines, nginx_error_lines"
cat <<'VECTOR'
  nginx_access:
    type: file
    include: [/var/log/nginx/access.log]
    read_from: end
  nginx_errors:
    type: file
    include: [/var/log/nginx/error.log]
    read_from: end
VECTOR
fi

cat <<'VECTOR'

transforms:
  journal_lines:
    type: remap
    inputs: [journal]
    source: |
      levels = ["emergency", "alert", "critical", "error", "warn", "notice", "info", "debug"]
      # string() fails on a missing field, so the next name applies. Datum refuses an empty service.
      unit = string(._SYSTEMD_UNIT) ?? string(.SYSLOG_IDENTIFIER) ?? "host"
      if ._TRANSPORT == "kernel" {
        unit = "kernel"
      }
      priority = to_int(string(.PRIORITY) ?? "6") ?? 6
      . = {
        "ts": format_timestamp!(.timestamp, "%Y-%m-%dT%H:%M:%S%.3fZ"),
        "product": "atlas",
        "source": "journal",
        "service": replace(unit, r'[^a-zA-Z0-9_./-]', "_"),
        "level": string(get(levels, [priority]) ?? "info") ?? "info",
        "message": truncate(to_string(.message) ?? "", limit: 8000),
        "attributes": {},
      }
VECTOR

# Access lines carry client addresses, so they stay on the node. Vector sends their counts and times.
if [ "$SERVICE" = "proxy" ]; then
cat <<'VECTOR'
  nginx_requests:
    type: remap
    inputs: [nginx_access]
    drop_on_error: true
    source: |
      request = object!(parse_json!(.message))
      status = to_string!(request.status)
      # A retried request lists one time for each upstream it tried. The last one answered.
      upstream = split(to_string(request.upstream_response_time) ?? "", ", ")
      . = {
        "status_class": slice!(status, 0, 1) + "xx",
        "request_time": to_float(request.request_time) ?? 0.0,
        "upstream_time": to_float(get(upstream, [-1]) ?? "") ?? 0.0,
      }
      # log_to_metric drops the whole event when one of its fields is missing, so slow is always set.
      .slow = if .request_time >= 1.0 { 1.0 } else { 0.0 }
  nginx_metrics:
    type: log_to_metric
    inputs: [nginx_requests]
    metrics:
      - type: counter
        field: status_class
        namespace: service
        name: proxy_requests_total
        tags:
          status_class: "{{ status_class }}"
      - type: counter
        field: request_time
        increment_by_value: true
        namespace: service
        name: proxy_request_seconds_total
      - type: counter
        field: upstream_time
        increment_by_value: true
        namespace: service
        name: proxy_upstream_seconds_total
      - type: counter
        field: slow
        increment_by_value: true
        namespace: service
        name: proxy_slow_requests_total
  nginx_error_lines:
    type: remap
    inputs: [nginx_errors]
    drop_on_abort: true
    source: |
      levels = {"error": "error", "crit": "critical", "alert": "alert", "emerg": "emergency"}
      parsed, error = parse_regex(.message, r'^\S+ \S+ \[(?P<level>\w+)\]')
      level = get(levels, [parsed.level]) ?? null
      if error != null || level == null {
        abort
      }
      . = {
        "ts": format_timestamp!(.timestamp, "%Y-%m-%dT%H:%M:%S%.3fZ"),
        "product": "atlas",
        "source": "nginx",
        "service": "openresty",
        "level": level,
        "message": truncate(to_string(.message) ?? "", limit: 8000),
        "attributes": {},
      }
VECTOR
fi

cat <<VECTOR

sinks:
  datum_metrics:
    type: prometheus_remote_write
    inputs: [$metric_inputs]
    endpoint: "$datum_url/v1/ingest/remote"
    auth:
      strategy: bearer
      token: "$DATUM_TOKEN"
    healthcheck:
      enabled: false
    # Datum takes 12 requests a minute on each route from one resource.
    batch:
      timeout_secs: 30
    request:
      concurrency: 1
      rate_limit_num: 10
      rate_limit_duration_secs: 60
    buffer:
      type: disk
      max_size: 268435488
      when_full: drop_newest
  datum_logs:
    type: http
    inputs: [$log_inputs]
    uri: "$datum_url/v1/logs/ingest"
    method: post
    encoding:
      codec: json
    # The JSON codec sends a batch as an array. Datum takes one {"lines": [...]} object per request.
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
echo "==> $SERVICE telemetry configured for $datum_url"
