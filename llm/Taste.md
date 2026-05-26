Guidelines for writing good code

1. Choose clean code over clever code
1. Write object oriented code as much as possible
1. Keep functions small, ideally 10 lines
1. Keep files small, between 100 and 300 lines
1. Keep directories or module small, fewer than 15 files
1. Avoid abbreviations. `vm` is allowed only when (a) it shadows a Frappe method name (`delete`) or (b) it is a local variable inside a five-line function. Doctype controller methods, module-level functions, and public helpers spell it out.
1. Use standard Frappe API as much as possible
1. Reuse. Write as little code as possible
1. Use Frappe UI, espresso design system for UI styling
1. Build the minimum working app, then iterate towards your goals
1. Always write tests, and make sure they work
1. One operation = one shell script = one Task row. Compose at the script level (heredocs, `set -euo pipefail`), not by chaining `run_task` calls in Python. If you have two scripts that always run back-to-back, merge them.
1. Scripts are the source of truth for server-side logic. Server-side logic lives in `scripts/*.sh`. Python calls scripts and parses their output. Do not encode server-side state machines in Python.
1. Every shell script in `scripts/` must be idempotent. Retry = re-run, no special repair mode.
1. Fail loud at the boundary; do not fall back. SSH failed? raise. DO API 5xx? raise. The operator retries by clicking the button.
1. Tests live next to the code they cover. `atlas/atlas/doctype/<x>/test_<x>.py` for controllers, `atlas/tests/test_<module>.py` for modules, `atlas/tests/e2e/phase_N.py` for end-to-end.


Refer to [Pilot, agent and deploy repositories in references](./references/README.md) for my personal taste and coding style.