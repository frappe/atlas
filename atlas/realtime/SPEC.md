# Realtime module specification

[App specification](../SPEC.md)

## Purpose

The realtime module bridges an authenticated Atlas console session to Metal.

## Ownership

`handlers.py` validates the console token and owns the WebSocket bridge lifecycle.

Metal owns the serial console. Atlas owns the browser session and its token.

## Tests

```sh
ruff check atlas
bench --site TEST_SITE run-tests --app atlas
```
