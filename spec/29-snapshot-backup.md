# Snapshot backup to S3

A `Virtual Machine Snapshot` is an LVM thin snapshot living in **one** thin pool
on **one** server ([05](./05-virtual-machine-lifecycle.md),
[07](./07-filesystem-layout.md)). That is instant and space-thin, but it is not
durable: lose the pool (a wiped disk, a re-imaged host, a reclaimed server) and
every snapshot on it is gone. This chapter adds an **off-host backup**: push a
point-in-time snapshot's bytes to **S3**, and pull them back later to rebuild the
same snapshot's on-host artifacts — so the snapshot survives the host that made
it.

## Scope (this iteration)

- **Same-VM rollback.** A backup restores onto the snapshot's **own** server and
  rolls its **own** VM back. Restoring to a *new* VM on *another* server (true
  cross-host disaster recovery) is a durable independent artifact — a bigger data
  model — and is deferred (see *Non-goals*).
- **Both kinds.** A **Cold** snapshot backs up its disk LV(s); a **Warm**
  snapshot also backs up its frozen memory pair (`vmstate.bin`/`mem.bin`) and the
  capture-time host signature, so a rehydrated warm golden can still be **cloned**
  (its sanctioned consumption path — [05](./05-virtual-machine-lifecycle.md),
  `Virtual Machine Snapshot.clone_to_new_vm`). Warm **in-place resume** onto the
  same VM is out of scope; warm restore rehydrates for cloning.

## The transport: controller-presigned URLs, host has no credentials

Hosts stay stdlib-only and credential-free (a spec goal —
[README](./README.md#operating-principles) *Few dependencies*; a host never holds
S3 keys). The controller already links `boto3` for TLS
([13](./13-tls.md), [`dns/route53.py`](../atlas/atlas/dns/route53.py)); it reuses
it here to **presign** short-lived `PUT`/`GET` URLs. The host does the byte
movement with **`curl`** alone — no `aws` CLI, no boto, no keys on disk:

```
Controller (boto3):  presign PUT/GET for each object key
        │  presigned URL (a time-limited bearer token; no static creds)
        ▼
Host (curl + zstd; NO dd, NO pipe — zstd reads/writes the block device via -o):
  upload:   zstd -o tmp /dev/atlas/atlas-snap-<id> ; sha256sum tmp ; curl -T tmp <put-url>
  restore:  curl -o tmp <get-url> ; sha256sum -c ; zstd -d --sparse -o /dev/atlas/atlas-snap-<id> tmp
```

This mirrors [`sync-image.py`](../scripts/sync-image.py), which already pulls
image artifacts with `curl` and verifies a `sha256`. The presigned URLs are
minted per Task on the controller, passed as Task variables (so they land in the
`Task.variables_dict` and the rendered command line), and **expire**
(`S3 Settings.presign_expiry_seconds`, default 3600). They are bearer tokens for
exactly one object + method for that window; nothing static leaks to the host.

`boto3` is presigned with the default `UNSIGNED-PAYLOAD` signature, so the host's
`curl` body is not part of the signature — a plain `PUT` with a `Content-Length`
uploads cleanly. That is also why the host **compresses to a temp file first**:
an S3 `PUT` needs a known length (AWS rejects `Transfer-Encoding: chunked`), and
the compressed size is unknown until it is written. Objects are processed **one
at a time** (compress → `sha256` → upload → delete the temp), so peak temp space
is the largest single *compressed* object, not the sum.

## Object layout

One snapshot maps to one S3 **key prefix**; each artifact is one object under it:

```
<S3 Settings.key_prefix>/<snapshot-name>/
    rootfs.img.zst          # root disk LV  (atlas-snap-<id>),      zstd
    data.img.zst            # data disk LV  (atlas-datasnap-<id>),  zstd — cold-with-data only
    vmstate.bin.zst         # Firecracker vmstate,                   zstd — warm only
    mem.bin.zst             # guest RAM,                             zstd — warm only
    host-signature.json     # capture-time CPU/kernel/FC,           raw  — warm only
```

The `.zst` suffix is the restore-side switch: `.zst` objects are piped through
`zstd -d`; a raw object (the tiny signature JSON) is written verbatim. Disk LVs
are captured as **raw block images** (`zstd -o` reads the whole block device
directly — no `dd`, no pipe), so a restore recreates a byte-identical LV
independent of the pool's thin metadata.

The **manifest** — one JSON row (`s3_objects`) recording each object's `name`,
`object`, `key`, `sha256` (of the *compressed* bytes), `compressed_bytes`,
`raw_bytes`, and `disk_gigabytes` (for LVs) — is written on a successful upload.
Restore reads it, re-presigns a `GET` per object, and hands the host everything
it needs; the controller never re-derives object names on the restore side.

## Upload

`Virtual Machine Snapshot.upload_to_s3()` (a desk button, whitelisted):

1. Guards: the snapshot is `Available`, its `server` exists, `S3 Settings` is
   configured. Re-upload is allowed (idempotent — the host `curl -T` overwrites).
2. Sets `s3_status = "Uploading"` and enqueues `run_upload` **after commit**, then
   returns — the byte movement is minutes of `dd`/`zstd`/`curl`, far too long for
   a web request (the same gunicorn-timeout lesson `promote_to_image` learned, and
   the same background-job shape). A lost/killed job leaves `s3_status =
   "Uploading"` and no manifest; the operator's retry re-drives it.
3. The job builds the object plan, presigns a `PUT` per object, and runs the
   **`upload-snapshot-s3`** Task on the snapshot's server. The host activates each
   source LV (`-K`, so activation-skipped snapshot LVs come up), `zstd -o`s it to a
   temp file (reading the block device directly — no `dd`, no pipe), `sha256sum`s
   it, `curl -T`s it to the presigned URL, deletes the temp, and repeats. It emits
   the per-object `sha256`/sizes.
4. On success the controller writes the manifest and sets `s3_status =
   "Uploaded"`, `s3_bucket`, `s3_key_prefix`, `s3_size_bytes`, `s3_uploaded_at`.
   On any host failure it sets `s3_status = "Failed"` and re-raises (loud at the
   boundary — the operator retries the button).

## Restore

`Virtual Machine Snapshot.restore_from_s3()` (a desk button, whitelisted):

1. Guards: `s3_status == "Uploaded"`, the `server` exists. For a **Cold**
   snapshot whose VM is being rolled back, the VM must be `Stopped` (the same
   guard `restore_to_vm` → `rebuild` enforces) — checked up front so a running VM
   fails *before* any download.
2. Sets `s3_status = "Restoring"` and enqueues `run_restore` after commit; same
   background-job reasoning as upload.
3. The job presigns a `GET` per manifest object and runs the
   **`restore-snapshot-s3`** Task. The host **rehydrates the on-host artifacts**:
   - Each disk LV is recreated as a fresh thin volume of the recorded
     `disk_gigabytes` (`ThinPool.create_thin`) at the exact name the row already
     records (`atlas-snap-<id>`, `atlas-datasnap-<id>`), then `curl | zstd -d |
     dd` fills it. Idempotent — an existing LV is reactivated and overwritten.
   - The warm memory pair is rebuilt under `warm_snapshot_directory(<id>)`
     (`vmstate.bin`, `mem.bin`, `host-signature.json`) with the same ownership
     `warm-snapshot-vm.py` uses (root-owned, `0644`, so any per-VM uid's jailed
     Firecracker can map it).
   - Each downloaded object is verified against its manifest `sha256` **before**
     decompression (`sha256sum -c`), exactly like `sync-image`.
4. The snapshot is now fully local again — `rootfs_path`, `data_rootfs_path`, and
   `memory_directory` already point at what was just rebuilt. The controller sets
   `s3_status = "Uploaded"` and then:
   - **Cold** → chains into the existing `restore_to_vm()` (rebuild-in-place), the
     rollback the operator asked for, and returns that Task.
   - **Warm** → stops after rehydration and tells the operator to **Clone to new
     VM** (warm's value is fan-out, not in-place rollback — same asymmetry as
     `promote_to_image`, which refuses warm).

Rehydration deliberately reuses the *existing* local paths so that after a
restore, every already-shipped local action (`restore_to_vm`, `clone_to_new_vm`)
works unchanged — the S3 round trip is invisible to them.

## Data model

- **`S3 Settings`** (a Single, the credential surface the operator fills in —
  mirrors [`Route53 Settings`](../atlas/atlas/doctype/route53_settings)):
  `bucket`, `region` (default `us-east-1`), `endpoint_url` (blank for AWS; set for
  S3-compatible stores — MinIO, DO Spaces), `access_key_id` (Data),
  `secret_access_key` (Password, read via `secrets.get_secret`), `key_prefix`
  (default `atlas/snapshots`), `presign_expiry_seconds` (default 3600). A **Test
  Connection** button (`test_connection`) `head_bucket`s to prove the credentials
  reach the bucket before any upload — the lightest read that exercises the same
  permissions, like `Route53Settings.test_connection`.
- **`Virtual Machine Snapshot`** gains a read-only *S3 Backup* section:
  `s3_status` (`""` / `Uploading` / `Uploaded` / `Restoring` / `Failed`),
  `s3_bucket`, `s3_key_prefix`, `s3_size_bytes` (Long Int — the total compressed
  bytes in S3), `s3_uploaded_at`, and `s3_objects` (the manifest JSON). No new
  DocType: a backup's lifecycle is tied to its snapshot row (see cleanup).

## Cleanup, integrity, idempotency

- **`on_trash`.** Deleting the snapshot row already `lvremove`s the local LV
  ([05](./05-virtual-machine-lifecycle.md)); when the row was uploaded it *also*
  deletes the S3 objects under its prefix, so a deleted snapshot leaves no paid
  orphan in the bucket. This is **best-effort**: an S3 error is logged, not
  raised, so a bucket hiccup never wedges a local delete. (The backup's lifecycle
  follows the row because the scope is same-VM rollback, not an independent
  archive — an archive that outlives its row is the deferred cross-host model.)
- **Integrity.** Every object carries a `sha256` of its compressed bytes;
  restore verifies it before writing — end-to-end, independent of S3's ETag.
- **Idempotency.** Re-uploading overwrites the objects; re-restoring re-fills the
  LVs. A failed upload/restore leaves a terminal `Failed`/`Restoring` status and
  is re-drivable by clicking the button again.

## Dependencies

- **Controller**: `boto3` (already required for TLS, [13](./13-tls.md)).
- **Host**: `zstd` (compression). `curl`, `lvm2`, `e2fsprogs` are already host
  deps; `zstd -d` was already relied on by `sync-image` kernel decompression, so
  this only makes the dependency explicit — `bootstrap-server.py` installs it.
  See [23-supply-chain.md](./23-supply-chain.md).

## Non-goals (deferred)

- **Cross-host disaster recovery.** Restoring to a brand-new VM on another server
  needs a backup record that outlives its source VM/server — a standalone
  `Snapshot Backup` DocType, not fields on the snapshot. Deferred.
- **Warm in-place resume.** Restoring a warm snapshot straight back onto its own
  running-state (staging the memory pair with a `READY` marker) is the
  clone/provision plumbing; warm restore here rehydrates for **Clone to new VM**.
- **Multipart / incremental.** One object per artifact, whole-image each time — no
  multipart upload, no changed-block/incremental backup, no server-side dedup.
- **A GC janitor.** Orphan-object sweeping beyond `on_trash` is out of scope
  (there is no pool janitor either — [07](./07-filesystem-layout.md)).
