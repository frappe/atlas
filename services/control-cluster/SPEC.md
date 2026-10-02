# Control cluster component specification

[Root specification](../../SPEC.md) · [Overview](README.md)

## Layout

| Path | Contents |
| --- | --- |
| `atlas_control/config.py` | `AuthConfig`, `ClusterConfig`, and the `[auth]` and `[cluster]` TOML parsers |
| `atlas_control/auth.py` | `Authentication` for bearer passwords and issuer-bound Ed25519 JWTs, and `Authorization` |
| `atlas_control/cluster.py` | `ClusterManager`, `Mutation`, `ClusterSnapshot`, and the `StateMachine` protocol |
| `atlas_control/routes.py` | The `/internal/cluster/*` peer routes |
| `atlas_control/docs.py` | The Scalar API reference at `/docs`, its OpenAPI schema, and route operation IDs |
| `tests/` | Authentication and cluster tests |

## Extension points

- A service passes its scopes, its user agent, and its constrained resources to `Authentication`. Only the proxy uses name constraints.
- A service implements `StateMachine`: `initial_state`, `is_serving`, `prepare`, `restore`, and `apply`. Heartbeat replies carry `is_serving`. `prepare` runs once on the leader with the serving members, for a choice such as placement. `apply` validates the mutation kind and value, and changes the service before it changes the state.
- A service mounts `create_cluster_router(cluster)` and serves its own public API. It calls `docs.add_routes(app)` to publish the API reference.

## Invariants

- Every mutation is safe to repeat. A failed write can exist on some nodes, and the caller sends it again.
- Clusters with up to 3 members need every acknowledgement. Clusters with 4 or 5 members need a majority.
- A token with a `tenant` claim is an Atlas API credential, and the package refuses it.
- The audience must be `<service>:<region ID>`, and the issuers must be `central` and `atlas:<region ID>`.

## Validation

From this directory, run `python -m pip install --editable '.[test]'`, `python -m ruff check .`, and `python -m pytest -q tests`. Also run the proxy control tests, because the proxy uses this package.
