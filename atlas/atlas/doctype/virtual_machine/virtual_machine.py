import uuid

import frappe
from frappe.model.document import Document

from atlas.atlas.networking import allocate_ipv6, derive_mac, derive_tap
from atlas.atlas.ssh import run_task_on_server

IMMUTABLE_AFTER_INSERT = ("server", "image", "vcpus", "memory_megabytes", "disk_gigabytes")


class VirtualMachine(Document):
	def autoname(self) -> None:
		# autoname() runs from set_new_name(), which is called by Document.insert()
		# after before_insert(). We assign the UUID here and derive the dependent
		# fields in before_validate() (which runs after set_new_name).
		self.name = str(uuid.uuid4())

	def before_validate(self) -> None:
		if self.is_new() and not self.mac_address:
			self.mac_address = derive_mac(self.name)
		if self.is_new() and not self.tap_device:
			self.tap_device = derive_tap(self.name)
		if self.is_new() and not self.ipv6_address:
			self.ipv6_address = allocate_ipv6(self.server)
		if self.is_new() and not self.status:
			self.status = "Pending"

	def validate(self) -> None:
		if self.is_new():
			return
		original = self.get_doc_before_save()
		if not original:
			return
		for field in IMMUTABLE_AFTER_INSERT:
			if getattr(self, field) != getattr(original, field):
				frappe.throw(f"{field} is immutable after insert")

	@frappe.whitelist()
	def provision(self) -> str:
		"""Run provision-vm.sh. Image must already be on the server."""
		if self.status not in ("Pending", "Failed"):
			frappe.throw(f"Cannot provision from {self.status}")

		self._assert_image_present()

		self.status = "Provisioning"
		self.save(ignore_permissions=True)
		frappe.db.commit()

		try:
			task = run_task_on_server(
				server=self.server,
				script="provision-vm.sh",
				variables=self._provision_variables(),
				virtual_machine=self.name,
				timeout_seconds=30,
			)
		except Exception:
			self.reload()
			self.status = "Failed"
			self.save(ignore_permissions=True)
			frappe.db.commit()
			raise

		self.reload()
		self.status = "Running"
		self.last_started = frappe.utils.now_datetime()
		self.save(ignore_permissions=True)
		return task.name

	@frappe.whitelist()
	def start(self) -> str:
		if self.status != "Stopped":
			frappe.throw(f"Cannot start from {self.status}")
		task = run_task_on_server(
			server=self.server,
			script="start-vm.sh",
			variables={"VIRTUAL_MACHINE_NAME": self.name},
			virtual_machine=self.name,
			timeout_seconds=30,
		)
		self.reload()
		self.status = "Running"
		self.last_started = frappe.utils.now_datetime()
		self.save(ignore_permissions=True)
		return task.name

	@frappe.whitelist()
	def stop(self) -> str:
		if self.status != "Running":
			frappe.throw(f"Cannot stop from {self.status}")
		task = run_task_on_server(
			server=self.server,
			script="stop-vm.sh",
			variables={"VIRTUAL_MACHINE_NAME": self.name},
			virtual_machine=self.name,
			timeout_seconds=30,
		)
		self.reload()
		self.status = "Stopped"
		self.last_stopped = frappe.utils.now_datetime()
		self.save(ignore_permissions=True)
		return task.name

	@frappe.whitelist()
	def restart(self) -> dict:
		"""Stop (if Running) then Start. Two Tasks."""
		if self.status not in ("Running", "Stopped"):
			frappe.throw(f"Cannot restart from {self.status}")
		stop_task = self.stop() if self.status == "Running" else None
		start_task = self.start()
		return {"stop_task": stop_task, "start_task": start_task}

	@frappe.whitelist()
	def delete_vm(self) -> str:
		if self.status == "Archived":
			frappe.throw("VM is already archived")
		task = run_task_on_server(
			server=self.server,
			script="delete-vm.sh",
			variables={"VIRTUAL_MACHINE_NAME": self.name},
			virtual_machine=self.name,
			timeout_seconds=60,
		)
		self.reload()
		self.status = "Archived"
		self.save(ignore_permissions=True)
		return task.name

	def _provision_variables(self) -> dict:
		image = frappe.get_doc("Virtual Machine Image", self.image)
		return {
			"VIRTUAL_MACHINE_NAME": self.name,
			"IMAGE_NAME": self.image,
			"KERNEL_FILENAME": image.kernel_filename,
			"ROOTFS_FILENAME": image.rootfs_filename,
			"VCPUS": str(self.vcpus),
			"MEMORY_MB": str(self.memory_megabytes),
			"DISK_GB": str(self.disk_gigabytes),
			"MAC_ADDRESS": self.mac_address,
			"TAP_DEVICE": self.tap_device,
			"VIRTUAL_MACHINE_IPV6": self.ipv6_address,
			"SSH_PUBLIC_KEY": self.ssh_public_key,
		}

	def _assert_image_present(self) -> None:
		image = frappe.get_doc("Virtual Machine Image", self.image)
		probe = run_task_on_server(
			server=self.server,
			script="probe-image-present.sh",
			variables={
				"IMAGE_NAME": image.image_name,
				"ROOTFS_FILENAME": image.rootfs_filename,
			},
			virtual_machine=self.name,
			timeout_seconds=30,
		)
		# Probe exits 0 when present; non-zero is caught as Failure and raises
		# via run_task_on_server before we get here. We only reach this line
		# on Success, so no extra assertion needed.
		_ = probe
