# Realtime console bridge specification

[App specification](../SPEC.md)

## Purpose

The realtime module bridges an authenticated Atlas console session to Metal.

## Ownership

`handlers.py` validates the console token and owns the WebSocket bridge lifecycle. `realtime/handlers.js` in the repository root is the same bridge for the node socketio backend. Pilot uses the python backend by default.

Metal owns the serial console. Atlas owns the browser session and its token.

`ConsoleSession.close` is the only session cleanup owner. A token is one use. The handler validates the stored connection, input encoding, input size, and terminal dimensions before it sends data to Metal.

Use [Atlas operations](../docs/operations.md) for Metal connectivity checks. The console bridge does not log the Metal authorization value.
