# Virtual machine module specification

[App specification](../SPEC.md)

[Human entry point](README.md)

## Purpose

The virtual machine module owns Atlas request records, placement, Metal transport, images, and user workflows.

## Ownership

Keep DocType methods as permission and lifecycle boundaries. Keep cross-document and Metal operations in `core/`.

Metal owns mutable virtual machine desired state and observed state. Atlas keeps only virtual views of this state.

## Invariants

- The Virtual Machine name is the Metal virtual machine ID.
- Atlas commits a draft before a create request.
- Atlas keeps an uncertain draft until Metal confirms presence or absence.
- Public IPv4 changes preserve the current intent version check.
- The Virtual Machine schema remains unchanged during this refactor.

## Tests

```sh
ruff check atlas
bench --site TEST_SITE run-tests --module atlas.vm.doctype.virtual_machine.test_virtual_machine
bench --site TEST_SITE run-tests --module atlas.vm.doctype.virtual_machine_image.test_virtual_machine_image
```
