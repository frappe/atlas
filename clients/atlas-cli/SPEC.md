# Operator CLI specification

The [operator CLI guide](../../docs/interfaces/api-clients.md#operator-cli) owns installation, authentication, and command usage.

`atlas_cli/cli.py` owns argument parsing, connection settings, pagination, and output. It calls the generated `atlas_client` operations. `atlas_cli/__main__.py` supports `python -m atlas_cli`.

Each command owns one HTTP client with a 30-second timeout. The API owns authorization and VM state. The CLI does not retry requests. List commands print output after all pages succeed and reject pagination that cannot advance.

Run `python -m unittest discover -s clients/atlas-cli/tests -v` from the repository root after installing both client packages. The tests use the generated client with a controlled HTTP transport.
