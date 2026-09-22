# HTTP API

For Go code, follow the repository [Go anti-pattern rules](../../llm/go-code-review-guide.md).

[metal SPEC](../SPEC.md) · detail: [internal/api/SPEC.md](../internal/api/SPEC.md)

`metald` serves the controller API. The complete reference, with every field and status, is the OpenAPI document the binary embeds: open `GET /docs` in a browser, or read `GET /docs/swagger.json`.

## Model

A mutation stores desired state and returns. It does not wait for the host. The controller polls `GET /v1/vms/{id}` until the desired and observed generations match, and reads `observed.error` when they stop moving.

Each VM response nests `desired` and `observed`, so one read shows both what was asked for and what the host reached.

`PUT /v1/vms/{id}/power` sets the power state with `{"state": "running"|"stopped"|"paused"}`. A `stopped` request is always a hard stop and removes saved VM state.

`PUT /v1/vms/{id}/compute` sets `cpu_millicores`, `memory_mib`, and `sleep_after_idle_seconds`. `1000` millicores equals one CPU core. The valid CPU range is 100 through 32000 millicores. The lower limit prevents impractical VM CPU quotas. The upper limit follows Firecracker's maximum of 32 guest vCPUs. A CPU or memory change needs a stopped VM. An idle timeout change is accepted in any state. `0` disables automatic idle shutdown. The response reports the value in `desired.compute`. A memory increase that the host cannot hold returns `409` with the code `insufficient_capacity`. A disk increase uses the same code.

`PUT /v1/vms/{id}/resize` stores CPU, memory, disk, and idle shutdown as one shape. It checks both increases before it changes the desired state.

The port 9000 listener requires the Atlas client certificate and accepts only its configured common name. Source-side migration routes are on the port 9001 coordination API, which requires a node certificate. Liveness and the documentation carry no VM data, but they sit behind the same listener.

Routes, status codes, and error mapping: [internal/api/SPEC.md](../internal/api/SPEC.md).

## Console modes

One endpoint serves two modes. `mode=tty` is the default.

| Mode | Session | History | Viewers |
|---|---|---|---|
| `tty` | The VM's serial console. It runs from VM start to VM stop. | Recent output replays on attach. | Many, capped. A viewer that stops reading is disconnected. |
| `ssh` | An SSH session created for this connection and closed with it. | None. | One. |

Both modes use the same framing. Binary frames carry terminal bytes in both directions. A text frame carries a control message, for example `{"resize":{"cols":120,"rows":40}}`. Full detail: [internal/console/SPEC.md](../internal/console/SPEC.md).

## Design notes

- Mutations are asynchronous, so a slow host never holds a controller connection open.
- Generations are the only completion signal, so every real change raises one.
- A response omits transport URLs, user data, and host paths: the controller cannot use them and must not store them.
