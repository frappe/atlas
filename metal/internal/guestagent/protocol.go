package guestagent

// UsagePort is the guest vsock port that answers with one usage report. Each
// job has its own port, so an old agent refuses a newer job. The ports are above
// 65535, so they cannot clash with a service that reuses a TCP port number.
const UsagePort = 100000
