# OCI rootfs component specification

[Root specification](../../SPEC.md) · Behavior: [OCI images](../../docs/storage/image-records.md#oci-images)

## Layout

```text
main.go        Command line
registry.go    Registry pull: reference, token, manifest, digest checks
rootfs.go      Layer unpack, guest-init install, ext4 file
upload.go      zstd copy, signed upload, build result
guest-init/    PID 1 of a VM from an OCI image, embedded in the CLI
```

## Usage

`make build` writes `dist/oci-rootfs-linux-amd64`. Run it as root on Linux with `mkfs.ext4` installed:

```console
oci-rootfs build -output nginx.ext4 nginx:1.29-alpine
```

## Invariants

- A layer unpacks through `os.Root`, because the archive is untrusted.
- A pull verifies every digest. Atlas passes a reference that is pinned to a digest.
- The zstd frame states its content size, because Metal sizes the disk from it.
- guest-init reaps all processes. Do not use the Go wait functions for its children.
- guest-init reads metadata with short timeouts, so a stop request finishes before the Metal stop timeout.

## Tests

There are no unit tests. Run `make build` and `go vet ./...`, then import an image on a development site.
