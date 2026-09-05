from cloudinit import sources, url_helper

METADATA_BASE = "http://169.254.169.254/latest"
TOKEN_TTL_SECONDS = "21600"


class DataSourceAtlas(sources.DataSource):
	dsname = "Atlas"

	def __init__(self, sys_cfg, distro, paths, ud_proc=None):
		sources.DataSource.__init__(self, sys_cfg, distro, paths, ud_proc)
		self.metadata = {}
		self.userdata_raw = None

	def _fetch_text(self, path, headers):
		response = url_helper.readurl(f"{METADATA_BASE}/{path}", headers=headers, timeout=5, retries=3)
		return response.contents.decode().strip()

	def _get_data(self):
		try:
			token = (
				url_helper.readurl(
					f"{METADATA_BASE}/api/token",
					request_method="PUT",
					headers={"X-metadata-token-ttl-seconds": TOKEN_TTL_SECONDS},
					timeout=5,
					retries=3,
				)
				.contents.decode()
				.strip()
			)
			headers = {"X-metadata-token": token}
			self.metadata["instance-id"] = self._fetch_text("meta-data/instance-id", headers)
		except Exception:
			return False

		try:
			self.metadata["local-hostname"] = self._fetch_text("meta-data/local-hostname", headers)
		except Exception:
			pass

		try:
			self.userdata_raw = url_helper.readurl(
				f"{METADATA_BASE}/user-data", headers=headers, timeout=5, retries=3
			).contents
		except Exception:
			self.userdata_raw = None

		return True

	def _get_subplatform(self):
		return "metadata (Atlas MMDS v2)"

	def get_instance_id(self):
		return self.metadata.get("instance-id")


datasources = [
	(DataSourceAtlas, (sources.DEP_FILESYSTEM, sources.DEP_NETWORK)),
]


def get_datasource_list(depends):
	return sources.list_from_depends(depends, datasources)
