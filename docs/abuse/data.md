# Abuse protection data

Status: planned. Atlas does not implement this yet. See [abuse protection](index.md).

## Records

Atlas keeps two abuse records:

- VM Restriction is the desired state of a restriction. See [layouts](layouts.md#vm-restriction).
- Abuse Settings is a single DocType. It holds only the log bucket endpoint, the bucket name and the write keys. See [layouts](layouts.md#abuse-settings).

- VM Restriction stores `virtual_machine` as Data, not as a Link. So a row stays after Atlas deletes the VM.
- `track_changes` on VM Restriction keeps the audit record of who restricted a VM and when.

Atlas keeps no other abuse data:

- Metal keeps the eBPF counters in memory until metald restarts. See [counters](metrics.md#counters).
- The `vm_abuse_*` metrics are in Datum. See [Cargo per-VM metrics ↗](https://github.com/frappe/cargo/blob/develop/docs/abuse-logs.md#per-vm-metrics).
- The NAT, SNI and proxy access logs are in the Cargo log bucket. See [log upload](contract.md#log-upload).
- Cases, signals, reports and public IP history are in Central. See the [Central abuse specification ↗](https://github.com/frappe/central/blob/develop/spec/ABUSE.md).

## Retention

| Data | Retention | Owner |
| --- | --- | --- |
| VM Restriction | 730 days after the restriction is `Lifted` or `Failed`. Atlas never deletes an active restriction. | Atlas cleanup job |
| `vm_abuse_*` metrics | 30 days, by the Datum table TTL | [Cargo per-VM metrics ↗](https://github.com/frappe/cargo/blob/develop/docs/abuse-logs.md#per-vm-metrics) |
| NAT, SNI and proxy access logs | 180 days, by the bucket lifecycle rules | [Cargo object layout ↗](https://github.com/frappe/cargo/blob/develop/docs/abuse-logs.md#object-layout) |

## Time source

Log times must be traceable for CERT-In. No NIC or NPL time server is set today (`metal/cmd/metald/config.go`). Point chrony on hosts and service VMs at `samay1.nic.in`, `samay2.nic.in` and `time.nplindia.org`. Check the hostnames first. See the [Central CERT-In specification ↗](https://github.com/frappe/central/blob/develop/spec/CERT_IN.md).
