# Metal Server provider package

This package defines the `ServerProvider` contract and registry. Provider packages implement remote resource operations.

Read the [provider guide](../../../../docs/region/provider-guide.md) for the contract, ownership rules, extension steps, and the Generic, Scaleway, and AWS structure.

Use absolute imports in this package. Keep Frappe document writes and transaction commits outside low-level provider components.
