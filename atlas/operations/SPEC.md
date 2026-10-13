# Operations module specification

[Atlas app specification](../SPEC.md)

Behavior: [Roll out a build](../../docs/region/hosts-and-providers.md#roll-out-a-build).

## Types

| Type | Owns |
|---|---|
| `MetaldUpdate` (DocType) | Pinned build, rollout settings, status, and Desk actions. |
| `MetaldUpdateServer` (DocType) | Server batch, status, versions, and upgrade SSH Task. |
| `run_metald_update_steps` | Runs one step of each Planning or Running update every 5 seconds. |
| `read_server_versions` | Reads one host and records a planning or check result. |

## Invariants

- A step locks the update row, works from stored row statuses, and only queues jobs. It makes no Metal or SSH call, so a lost or repeated step is safe.
- A step does not start an upgrade while the Metal Server `metald_job_id` job runs.
- Metal Server stores no version. Versions come from `MetalClient.get_versions`, and a `404` means that the server needs the build.
- Atlas keeps each File that an update links, so a duplicate can roll the servers back.
