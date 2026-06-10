__version__ = "0.0.1"

from atlas.atlas.atlas_settings import (
	get_provider,
	get_ssh_key,
	get_ssh_private_key_path,
	provision,
)
from atlas.atlas.doctype.server.server import sync_scripts_to_all
