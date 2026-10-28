---
name: incident-investigation
description: Finds the root cause of a problem in an Atlas region (VMs, Metal hosts, metald, Firecracker, ZFS, network, alerts) from Datum telemetry first and hosts second, and writes a short report. Use for any alert, outage, slow host, unreachable or failed VM, or "why is X happening" question.
---

# Investigate an Atlas problem

Find the first failed step and the component that owns it. Every claim needs evidence from a tool.

Reference files, read with `load_skill(name="incident-investigation", file=...)`:

- `playbooks.md`: checks for each symptom. Read it after step 1.
- `telemetry.md`: Datum metric names, counter rules, and SQL patterns. Read it before your first `query_telemetry`.

## Workflow

Copy this checklist into your notes and follow it in order:

```text
- [ ] 1 Scope: what, where, since when
- [ ] 2 Verify the symptom is real
- [ ] 3 What changed
- [ ] 4 Hypotheses: 2 to 4
- [ ] 5 Test each hypothesis
- [ ] 6 Report
```

**1 Scope.** Call `list_failing_alert_checks` and `get_telemetry_freshness`. Call `find_resources` for each name, IP address, or tenant in the problem, then `describe_host` or `describe_virtual_machine`. A resource with data older than 300 seconds is blind in Datum: use the host tools for it.

**2 Verify.** An alert is a claim, not a fact. Get the raw samples behind it with one query. For a `_total` counter, call `get_counter_increase`. Never compare two different metrics. If the raw data does not show the problem, the alert is the problem: report it as a false alert.

**3 What changed.** Call `get_host_changes` for each affected host. It shows versions, metald restarts, reboots, field changes such as maintenance, SSH tasks, drains, and Metald Updates. Also call `list_recent_errors`.

**4 Hypotheses.** Write 2 to 4 possible causes in different components. Do not keep only the first idea.

**5 Test.** For each hypothesis, run the one query or host read that can prove it false. Compare with a normal window, such as the same hour yesterday. Read a host over SSH only for data that Datum does not have: the Firecracker log, the VM journal, live state, or a blind host. If a hypothesis is true, look one level deeper for the cause of that cause.

Stop when one cause explains every observation. Before you stop, name one observation that the cause does not explain. If there is one, go back to step 4.

## Report

Write in ASD-STE100 Simplified Technical English. Load the `simplified-technical-english` skill for the rules.

Write only the report. No preamble, no restated question, no raw tool output. At most 25 lines.

When there is a problem, use this template exactly. Draw the cause chain and the timeline in ASCII, inside a `text` code block:

````markdown
**<Component>: <what is broken>, since <YYYY-MM-DD HH:MM UTC>**

Impact: <hosts, VMs, tenants, services>
Confidence: <high | medium | low>, <reason in at most 10 words>

```text
CAUSE
  [<component>] <first failure> --> [<component>] <effect> --> [<user-visible>] <impact>

TIMELINE (UTC)
  <MM-DD HH:MM>  <source>  <event>
  <MM-DD HH:MM>  <source>  <event>
```

Evidence
- <tool or query key>, <time range>: <fact>

Ruled out
- <hypothesis>: <why, in one line>

Next
1. [read] <check for the engineer>
2. [change] <action that changes state>
````

Limits: at most 5 evidence lines, 3 ruled-out lines, and 4 next steps. Use plain ASCII characters only in the code block.

When there is no problem, write one bullet: `OK: <what you checked>. No fault found.` Add one bullet of evidence if it helps.

For a reply in the chat before the report, write at most 5 bullets: what you found, and what you check next.

Never print a credential. Keep host names, VM names, and IP addresses.
