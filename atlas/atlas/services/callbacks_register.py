"""Register every services handler on core's callback registry. Named by the
`services_callbacks` hook in hooks.py; core imports this by that string (never a
literal `services` import) on first dispatch, so importing it here wires the
whole PaaS side to the core lifecycle events without a core→services edge.

Add one `callbacks.register(...)` line per services handler as the core→PaaS
edges are inverted.
"""

from __future__ import annotations

from atlas.atlas.core import callbacks
from atlas.atlas.services import routing

callbacks.register("vm.address_changed", routing.on_vm_address_changed)
