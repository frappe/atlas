---
name: incident-investigation
description: Investigates alerts, outages, slow hosts, failed or unreachable VMs, and other problems in an Atlas region (metald, Firecracker, ZFS, network, WireGuard). Uses Datum telemetry, resource state, changes, and host evidence to find the first failure and its owner. Use for any alert or "why is X happening" question.
---

# Investigate an Atlas incident

Find what failed first, why it failed, what it affected, and what to do next. Every factual claim needs tool evidence.

Start with Datum. Use host tools only when Datum cannot answer the question. Choose checks that separate competing explanations with the least work.

Reference files, read with `load_skill(name="incident-investigation", file=...)`:

- `telemetry.md`: Datum metrics, counter rules, SQL patterns, and freshness. Read before the first `query_telemetry`.
- `playbooks.md`: symptom-specific checks. Read after step 1.

Before the report, load the separate `simplified-technical-english` skill.

## Workflow

Copy this checklist into your notes and follow it:

```text
- [ ] 1 Scope
- [ ] 2 Validate the symptom
- [ ] 3 Build the timeline
- [ ] 4 Form hypotheses
- [ ] 5 Test and narrow
- [ ] 6 Challenge the conclusion
- [ ] 7 Report
```

### 1. Scope

Call `list_failing_alert_checks` and `get_telemetry_freshness`. Call `find_resources` for each name, IP address, or tenant in the problem. Use `describe_host` or `describe_virtual_machine` when the resource state is needed.

Find the symptom, affected resources, first observed failure time in UTC, and current state: ongoing, recovered, intermittent, or unknown.

Telemetry older than 300 seconds is stale. Use host tools when possible. Missing data is not evidence of health.

If the affected resource is unknown, report the limit. Do not invent a scope.

### 2. Validate the symptom

An alert is a signal, not proof. Check raw samples in the alert window.

- For `_total` counters, use `get_counter_increase`.
- Do not compare unrelated metrics.
- Check labels, timestamps, and sample coverage.

| Class | Meaning |
| --- | --- |
| Confirmed | Raw data shows the fault. |
| Not reproduced | Complete data does not show the fault. |
| Unobservable | Data is missing or stale. |
| False alert | Evidence shows an error in the alert query or configuration. |

Missing data alone does not make an alert false.

### 3. Build the timeline

Call `get_host_changes` for each affected host and `list_recent_errors`.

`get_host_changes` includes versions, metald restarts, reboots, maintenance changes, SSH tasks, drains, and Metald Updates.

Find the first abnormal event and the first user-visible failure. Use UTC. Account for telemetry collection delay. If event order is uncertain, say so.

### 4. Form hypotheses

When the cause is uncertain, form 2 to 4 plausible hypotheses. Use different components when evidence permits.

For each hypothesis, identify:

- The failure mechanism.
- The evidence expected if it is true.
- One observation that would prove it false.

If a failure is already proven, investigate its cause instead of inventing alternatives.

### 5. Test and narrow

Run the test that can rule out the most competing explanations first.

For each test, use the smallest query or host read that answers one question. Compare with an unaffected resource or a normal time window when useful.

Use SSH only for information Datum does not have: Firecracker logs, VM journals, live kernel or network state, or stale telemetry. Avoid full log dumps and repeated queries that cannot change the conclusion.

Correlation is not causation. A restart near a failure does not prove that it caused the failure.

When evidence supports a cause, follow the dependency chain upstream. Keep these separate:

- **Trigger:** Event that started the incident.
- **Root cause:** Fault that explains the failure.
- **Propagation:** How the fault affected other components.
- **Impact:** What users or infrastructure experienced.

### 6. Challenge the conclusion

Check that the cause explains the symptoms, agrees with the timeline, has supporting evidence, and belongs to the named component.

If an important observation remains unexplained, return to step 4 or lower confidence. Do not force one cause when two independent failures exist.

| Outcome | Condition | Confidence |
| --- | --- | --- |
| Confirmed cause | Direct evidence shows the mechanism. Timing and scope agree. No important contradiction. | High |
| Probable cause | Independent observations support it. Alternatives are weaker. Direct proof is unavailable. | Medium |
| Blocked | Necessary telemetry or host access is unavailable. | Low |
| No fault found | Checks are healthy in the window, with sufficient coverage. | High |

Use low confidence when evidence is limited or competing explanations remain. Correlated metrics alone do not increase confidence.

After two rounds of testing without progress, review the scope and hypotheses before running more queries. Do not stop a critical investigation only because of this limit.

## Changes

Change state only with change tools. The tool asks for approval when needed.

Mark state-changing recommendations with `[change]`. If damage continues, recommend safe containment first. Do not recommend a change without supporting evidence.

## Report

Write in ASD-STE100 Simplified Technical English. Write only the report. No preamble, repeated question, internal reasoning, or raw tool output.

Use bullets by default. Use tables for 2 or more resources with the same fields. Use an ASCII diagram only for the cause chain, in a `text` code block. Use plain ASCII characters inside that diagram.

Omit empty sections. Keep host names, VM names, and IP addresses. Never print credentials.

### Incident found

````markdown
**<Component>: <failure>, since <YYYY-MM-DD HH:MM UTC>**

- Status: <ongoing | recovered | intermittent | unknown>
- Impact: <affected resources or services>
- Cause: <confirmed or probable mechanism>
- Confidence: <high | medium | low>, <reason>
- Containment: <action that stops damage, if ongoing>

```text
[<trigger>] --> [<first failure>] --> [<propagation>] --> [<impact>]
```

| Resource | State | Measure | Since (UTC) |
| --- | --- | --- | --- |
| <name> | <state> | <value> | <MM-DD HH:MM> |

| Time (UTC) | Source | Event |
| --- | --- | --- |
| <MM-DD HH:MM> | <tool or record> | <event> |

**Evidence**
- <tool or query key>, <time range>: <observation>

**Ruled out**
- <hypothesis>: <evidence against it>

**Unexplained**
- <observation or missing evidence>

**Next**
1. [read] <check needed to confirm the cause>
2. [change] <supported corrective action>
````

Include a confirmation check only when necessary. Include a corrective action only when evidence supports it.

### No fault found

```markdown
- OK: <resources and time window checked>. No fault found.
- Evidence: <supporting measurement, if useful>
```

### Blocked

```markdown
- Status: Cause not confirmed.
- Known: <verified observations>
- Missing: <required telemetry or access>
- Next: [read] <check that can resolve the uncertainty>
```

## Progress updates

Before the final report, give short updates with what is confirmed, what is unknown, and what you will check next.

Use a table when comparing 2 or more resources. Never present an untested hypothesis as a fact.
