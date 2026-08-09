# Phases 1–5 migration plan (Atlas → satellite)

Consolidated from five parallel design agents (one per phase). Phase 0 + polish are
**done, verified, pushed**. This captures the executable plan for the rest — and, more
importantly, the **decisions and real-fleet gaps** that block a "just do it" cutover.

## Cross-cutting prerequisites (shared by ≥2 phases)

- **`satellite/ssh.py`** — DONE (committed): `stdin` on `run_host`/`run_guest`, `scp_guest`,
  `run_host_addr`.
- **Mirror + read-API additions** (each widens Atlas's deliberately service-free read API —
  weigh per field):
  - `Server.ipv6` mirror field + `get_server` returns it — **base networking, safe** (mesh needs the wg endpoint). [Phase 1]
  - `Virtual Machine.reserved_ipv4` — for the proxy wildcard A-record. Alt: carry in the proxy binding config. [Phase 2/3]
  - `build_mode` — service-ish; alt: satellite derives it from the `image` name string. [Phase 4]
- **Region/domain state** satellite lacks: a `Region Domain` (Phase 2) that Phase 5 grows into the full `Root Domain`; a `Satellite Settings` single (`tls_provider_type`, `dns_provider_type`, `region`, `root_domain`, `default_bench_snapshot`).
- **Ordering is mandatory:** 1 → 2 → 3 → 5, with 4 alongside. Phase 2 stubs proxy's 4 map-builders to `{}` so Phase-3 modules still import; Phase 3 owns `satellite.services.proxy.{proxy_vms,wildcard_targets,push_cert}` that Phase 5 imports.

## Decisions required BEFORE execution (these are yours)

1. **Central re-point (Phase 4).** `central_report.py` STAYS in Atlas but depends on
   `front_door`→`Pilot`/`Site`. **Resolution A** (spec-literal): Central talks to satellite;
   delete `create_site`/`create_vm`/`get_site` + the Pilot/Site half of `central_report` +
   `api/site.py` from Atlas — large, cross-cutting. **Resolution B** (recommended, keeps
   every commit green): move the deploy *logic* to satellite additively, leave the Atlas
   Site/Pilot doctypes + Central seam until a dedicated "Central re-point" phase.
2. **`build_mode` / Image Build ownership (Phase 4).** Deleting `build_mode` transitively hits
   `Virtual Machine Snapshot` + `Virtual Machine Image` (provisioner-owned) and `Image Build`
   needs VM-provision + snapshot ops **satellite can't do** (Atlas exposes no execution API —
   your decision). **Recommend: keep `build_mode` + Image Build in Atlas** as a provisioner
   image attribute; only remove `pilot_credential_id` + the guest-deploy reads in Phase 4.
   This deviates from the spec's "delete build_mode" one-liner.
3. **Migration cutover sequencing (Phase 1 + 3).** `migration.py` re-points the private
   plane + proxies after a change-address cutover via `host_mesh.sequenced_migration_cutover`.
   Post-split, Atlas migration can't sequence satellite's overlay. Option (a): accept a
   transient double-advertise (converging reconcile heals in one tick) + TODO; option (b):
   an Atlas `vm.migrating{source,target}` webhook satellite turns into a sequenced cutover.
4. **`ROUTING_BASE_URL` guest cutover (Phase 2).** The guest self-routing client must be
   re-pointed from Atlas to the satellite site (or it 404s). A live-wiring change.
5. **`Satellite Settings` single vs. per-`Atlas`-record** vendor/region fields (Phase 5).

## Per-phase summary (full drafts: see session transcript / re-run the agents)

### Phase 1 — mesh + gateway/VPN
- **Create:** `satellite/networking.py` (VERBATIM port of the HKDF-from-UUID derivations —
  bit-identical or the mesh mis-routes; golden-vector parity test), `satellite/host_mesh.py`
  (cross-host reconcile off the mirror), rewritten `services/mesh.py` (REAL wg reconcile:
  apply/withdraw reconcile the whole fabric), `services/gateway.py` (port of
  `customer_gateway.py` + eBPF guard + host scripts), `VPN Peer`/`VPN Tunnel` doctypes,
  `reconcile_all_meshes` scheduler sweep.
- **Delete from Atlas:** `host_mesh.py`, `customer_gateway.py`, `api/tunnel.py`, `wireguard.py`,
  vpn doctype dirs; from `virtual_machine.py`: `is_gateway`, `_reconcile_host_mesh`(+2 calls),
  `deploy_gateway`, `_revoke_tunnels`, `_revoke_vpc_peers`, gateway half of `validate_infra_role`;
  `hooks.py` host-mesh cron + vpn `doctype_js`; `migration.py` `_repoint_private_plane` (decision 3).
- **Unverifiable here:** the cross-host AllowedIPs fabric (N=1 mesh has no peers) — only the
  pure `render_wg_mesh_config` is unit-testable.

### Phase 2 — routing
- **Create:** doctypes `Subdomain`, `Custom Domain`, `Port Mapping`, `Subdomain Denylist`,
  `Bench Routing Audit`(MyISAM), `Region Domain`; modules `routing/{region,labels,desired,ports,api}.py`,
  `services/routing.py` (`routing`/`routing-proxy` handlers, `reconcile_proxy_fleet`). A proxy =
  a `routing-proxy` binding; its `config` carries `{public_ipv4, region}`.
- **Delete from Atlas:** `bench_routing.py`, `custom_domain_label.py`, the 5 doctype dirs; from
  `virtual_machine.py`: `_delete_subdomains`/`_clear_subdomain_references`/`_delete_custom_domains`
  (+2 terminate calls); re-point `ROUTING_BASE_URL` (decision 4); gut the denylist `after_migrate`.
  KEEP `subdomain_label.py` (Phase 4 caller), `proxy.py`/`tcp_proxy.py` (Phase 3) — stub the 4
  map-builders to `{}`.
- **Unverifiable here:** byte-identity of canonical maps vs. a live proxy; `stream-admin` verbs.

### Phase 3 — proxy
- **Create:** `services/proxy.py` merging HTTP (`proxy.py`) + L4 (`tcp_proxy.py`) reconcile over
  `run_guest` reading the Phase-2 routing doctypes; `push_cert`/`wildcard_targets`/`read_live_maps`.
- **Delete from Atlas:** `tcp_proxy.py`(+test) cleanly; `read_proxy_maps`/`_deprovision_proxy`/`is_proxy`
  + proxy JS from `virtual_machine.py`. `proxy.py` can only be *fully* deleted once `build_proxy`
  (image, Phase 4), `migration.py` re-point (decision 3), and the TLS caller (Phase 5) are resolved —
  until then reduce it to `build_proxy`/`regenerate_placeholder_cert`.
- **Unverifiable here:** the SNI vs. port `stream-admin` verb split, cert reload, wildcard DNS — need a real proxy fleet.

### Phase 4 — bench/site/pilot
- **Create:** doctypes `Site`, `Pilot` (+ maybe `Image Build`, but recommend deferring — decision 2),
  `Settings` single; `satellite/bench.py` + `services/{site,pilot}.py` driving the committed
  `bench/deploy-site.py` over `run_guest`/`scp_guest`. Copy the `bench/` tree into satellite (stdlib-only).
  `Site Request` is DEAD CODE — delete-only.
- **Delete from Atlas:** per decision 1 (A: doctypes + Central Pilot/Site seam; B: just
  `pilot_credential_id` + `site_request` dir). `build_mode` stays (decision 2).
- **Unverifiable here:** the whole thing needs a real bench/site + a Central.

### Phase 5 — TLS/DNS/domain
- **Create:** doctypes `Root Domain`, `TLS Certificate`, `Lets Encrypt Settings`, `Route53 Settings`,
  `Satellite Settings`; modules `satellite/tls/{__init__,base,letsencrypt,self_managed,zerossl,runner,certs}.py`
  + `satellite/dns/{__init__,base,route53}.py` (near-verbatim ports; certbot runs as a LOCAL
  subprocess on the satellite node, not over SSH — PEMs land there then push to proxies via
  `run_guest`). `services/tls.py` handler + `renew_expiring` daily scheduler.
- **Delete from Atlas:** `tls/`, `dns/`, the 6 doctype dirs, `scripts/issue-cert.py`, `local_task.py`
  (if sole caller), the daily `renew_expiring` scheduler + the 4 TLS `doctype_js`, and the
  `tls_provider_type`/`dns_provider_type` Atlas Settings fields (they MOVE).
- **Unverifiable here:** certbot issuance, Route53 UPSERT, cert push — need real ACME/Route53 + a proxy.

## Bottom line
The design is complete and sound. Faithful *execution* is a large multi-commit effort gated on
the five decisions above and on real fleets (multi-host, proxy, ACME/Route53, bench) that the
one-host dev setup can't provide. Recommended path: resolve decisions 1–2 first (they shape the
most code), then land phases in order 1→2→3→(4)→5, each behind its own verify-what-you-can gate.
