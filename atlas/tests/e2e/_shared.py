"""Backwards-compatibility shim. The e2e helpers now live in four small modules:

- `_config.py`  — site-config readers, DEFAULT_IMAGE, ephemeral key
- `_droplets.py` — droplet lifecycle, shared-server reuse, phase() context
- `_tasks.py`   — Task-row helpers, assert_probe
- `_image.py`   — Virtual Machine Image helpers

This shim keeps `from atlas.tests.e2e._shared import …` working for callers
that haven't migrated, including operator-facing `bench execute` paths.
"""

from atlas.tests.e2e._config import (
	DEFAULT_IMAGE,
	SWEEP_AGE_SECONDS,
	TAG,
	MissingConfig,
	ephemeral_public_key,
	get_client,
	get_image,
	get_phase1_connection,
	get_region,
	get_size,
	get_ssh_key_id,
	get_ssh_private_key,
)
from atlas.tests.e2e._droplets import (
	cleanup_droplet,
	create_test_droplet,
	ensure_bootstrapped_server,
	ensure_e2e_provider,
	phase,
	server_is_reachable,
	sweep_old_droplets,
	teardown_all,
)
from atlas.tests.e2e._image import (
	ensure_default_image_row,
	ensure_image_on_server,
)
from atlas.tests.e2e._tasks import (
	assert_probe,
	expect_validation_error,
	mark_orphan_tasks_failure,
	wait_for_task,
)

__all__ = [
	"DEFAULT_IMAGE",
	"SWEEP_AGE_SECONDS",
	"TAG",
	"MissingConfig",
	"assert_probe",
	"cleanup_droplet",
	"create_test_droplet",
	"ensure_bootstrapped_server",
	"ensure_default_image_row",
	"ensure_e2e_provider",
	"ensure_image_on_server",
	"ephemeral_public_key",
	"expect_validation_error",
	"get_client",
	"get_image",
	"get_phase1_connection",
	"get_region",
	"get_size",
	"get_ssh_key_id",
	"get_ssh_private_key",
	"mark_orphan_tasks_failure",
	"phase",
	"server_is_reachable",
	"sweep_old_droplets",
	"teardown_all",
	"wait_for_task",
]
