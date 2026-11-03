from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from atlas_client.client import AuthenticatedClient

from atlas_cli.cli import main


class TestCLI(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = {
            "ATLAS_BASE_URL": "https://atlas.example.test",
            "ATLAS_TOKEN": "test-cli-token",
            "ATLAS_TENANT_ID": "7",
        }
        self.virtual_machine = {
            "id": "vm-7-00001",
            "tenant_id": 7,
            "architecture": "amd64",
            "cpu_millicores": 1000,
            "memory_mib": 1024,
            "disk_mib": 4096,
            "created_at": 0,
            "image_id": "image-1",
            "is_termination_protected": False,
            "public_ipv4": None,
            "public_ipv6": None,
            "sleep_after_idle_seconds": 0,
            "tags": {},
        }

    def _invoke(self, arguments: list[str], responses: list[httpx.Response | Exception]) -> tuple[int, str, str, Mock]:
        output, errors = io.StringIO(), io.StringIO()
        transport = Mock(side_effect=responses)

        def create_client(**settings: object) -> AuthenticatedClient:
            return AuthenticatedClient(**settings, httpx_args={"transport": httpx.MockTransport(transport)})

        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch("atlas_cli.cli.AuthenticatedClient", side_effect=create_client),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            status = main(arguments)
        return status, output.getvalue(), errors.getvalue(), transport

    def test_host_list_collects_pages_with_tenant_and_tag(self) -> None:
        hosts = [
            {"id": f"host-{index}", "title": f"metal-{index}", "status": "active", "tags": {"pool": "general"}}
            for index in range(2)
        ]
        responses = [
            httpx.Response(200, json={"items": [host], "has_more": index == 0, "limit": 1, "offset": index})
            for index, host in enumerate(hosts)
        ]
        status, output, errors, transport = self._invoke(
            ["--tenant-id", "0", "hosts", "list", "--limit", "1", "--tag", "pool:general", "--json"], responses
        )

        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(json.loads(output)["items"], hosts)
        self.assertEqual(transport.call_count, 2)
        for index, call in enumerate(transport.call_args_list):
            request = call.args[0]
            self.assertEqual(request.url.params["offset"], str(index))
            self.assertEqual(request.url.params["tag"], "pool:general")
            self.assertEqual(request.headers["X-Tenant-ID"], "0")
            self.assertEqual(request.headers["Authorization"], "Bearer test-cli-token")
            self.assertEqual(request.extensions["timeout"]["read"], 30.0)

    def test_list_failure_discards_partial_output(self) -> None:
        responses = [
            httpx.Response(
                200,
                json={
                    "items": [{"id": "host-1", "title": "metal-1", "status": "active", "tags": {}}],
                    "has_more": True,
                    "limit": 1,
                    "offset": 0,
                },
            ),
            httpx.Response(502, text="Upstream unavailable"),
        ]
        status, output, errors, transport = self._invoke(["hosts", "list", "--limit", "1", "--json"], responses)

        self.assertEqual((status, output, transport.call_count), (2, "", 2))
        self.assertIn("HTTP 502", errors)

    def test_rejects_pagination_that_cannot_advance(self) -> None:
        for changes in ({"limit": 0}, {"offset": 1}, {"has_more": True}, {"has_more": "false"}):
            with self.subTest(changes=changes):
                page = {"items": [], "has_more": False, "limit": 100, "offset": 0, **changes}
                status, output, errors, transport = self._invoke(["hosts", "list"], [httpx.Response(200, json=page)])
                self.assertEqual((status, output, transport.call_count), (2, "", 1))
                self.assertIn("invalid pagination", errors)

    def test_virtual_machine_list_formats_capacity_and_state(self) -> None:
        virtual_machine = {**self.virtual_machine, "last_known_state": "running", "state_synced_at": None}
        page = {"items": [virtual_machine], "has_more": False, "limit": 100, "offset": 0}
        status, output, errors, _ = self._invoke(["vms", "list"], [httpx.Response(200, json=page)])

        self.assertEqual((status, errors), (0, ""))
        for value in ("vm-7-00001", "running", "1000m", "1024 MiB", "4096 MiB"):
            self.assertIn(value, output)

    def test_actions_report_requests_without_waiting_for_completion(self) -> None:
        for action in ("start", "stop", "pause", "resume", "restart"):
            with self.subTest(action=action):
                status, output, errors, transport = self._invoke(
                    ["vms", action, "vm-7-00001"], [httpx.Response(202, json=self.virtual_machine)]
                )
                self.assertEqual((status, errors), (0, ""))
                self.assertEqual(output, f"{action} requested for vm-7-00001\n")
                transport.assert_called_once()
                self.assertEqual(
                    transport.call_args.args[0].url.path, f"/api/atlas/virtual-machines/vm-7-00001/actions/{action}"
                )

    def test_action_timeout_is_not_retried(self) -> None:
        status, output, errors, transport = self._invoke(
            ["vms", "restart", "vm-7-00001"], [httpx.ReadTimeout("test-cli-token")]
        )

        self.assertEqual((status, output), (2, ""))
        self.assertIn("check the VM state", errors)
        self.assertNotIn("test-cli-token", errors)
        transport.assert_called_once()

    def test_get_distinguishes_current_and_desired_state(self) -> None:
        specification = json.loads(
            (Path(__file__).resolve().parents[2] / "openapi" / "atlas-client.json").read_text(encoding="utf-8")
        )
        detail = specification["components"]["schemas"]["VirtualMachineDetailResponse"]["examples"][0]
        detail.update(current_state="stopped", desired_state="running")
        for output_options in ([], ["--json"]):
            with self.subTest(output_options=output_options):
                status, output, errors, _ = self._invoke(
                    ["vms", "get", detail["id"], *output_options], [httpx.Response(200, json=detail)]
                )
                self.assertEqual((status, errors), (0, ""))
                if output_options:
                    response = json.loads(output)
                    self.assertEqual(response["current_state"], "stopped")
                    self.assertEqual(response["desired_state"], "running")
                    self.assertEqual(response["compute"], detail["compute"])
                else:
                    self.assertIn("current state: stopped", output)
                    self.assertIn("desired state: running", output)

    def test_api_error_message_redacts_the_token(self) -> None:
        response = httpx.Response(
            403, json={"error": {"code": "forbidden", "message": "Rejected test-cli-token", "fields": []}}
        )
        status, output, errors, transport = self._invoke(["vms", "restart", "vm-7-00001"], [response])

        self.assertEqual((status, output), (2, ""))
        self.assertIn("HTTP 403: Rejected [redacted]", errors)
        self.assertNotIn("test-cli-token", errors)
        transport.assert_called_once()

    def test_invalid_response_does_not_print_a_traceback(self) -> None:
        for response in (httpx.Response(200, text="<html>Proxy error</html>"), httpx.Response(200, json={})):
            with self.subTest(response=response):
                status, output, errors, _ = self._invoke(["vms", "get", "vm-7-00001"], [response])
                self.assertEqual((status, output), (2, ""))
                self.assertIn("invalid response", errors)
                self.assertNotIn("Traceback", errors)

    def test_connection_errors_do_not_expose_credentials(self) -> None:
        status, output, errors, _ = self._invoke(["hosts", "list"], [httpx.ConnectError("test-cli-token")])

        self.assertEqual((status, output), (2, ""))
        self.assertIn("connection", errors)
        self.assertNotIn("test-cli-token", errors)

    def test_flags_override_environment(self) -> None:
        self.environment.update(ATLAS_BASE_URL="invalid", ATLAS_TENANT_ID="invalid")
        page = {"items": [], "has_more": False, "limit": 100, "offset": 0}
        status, output, errors, transport = self._invoke(
            ["--base-url", "https://other.example.test", "--tenant-id", "0", "hosts", "list", "--json"],
            [httpx.Response(200, json=page)],
        )

        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(json.loads(output), {"items": []})
        self.assertEqual(transport.call_args.args[0].url.host, "other.example.test")
        self.assertEqual(transport.call_args.args[0].headers["X-Tenant-ID"], "0")

    def test_invalid_environment_fails_before_any_request(self) -> None:
        for name, value in (
            ("ATLAS_BASE_URL", "file:///etc/passwd"),
            ("ATLAS_BASE_URL", "https://user:password@atlas.example.test"),
            ("ATLAS_TOKEN", ""),
            ("ATLAS_TOKEN", "token\nwith-newline"),
            ("ATLAS_TENANT_ID", "invalid"),
            ("ATLAS_TENANT_ID", "-1"),
            ("ATLAS_TENANT_ID", "4294967296"),
        ):
            with self.subTest(name=name, value=value):
                environment = self.environment.copy()
                self.environment[name] = value
                status, output, errors, transport = self._invoke(["hosts", "list"], [])
                self.environment = environment
                self.assertEqual((status, output), (2, ""))
                self.assertIn("atlasctl:", errors)
                transport.assert_not_called()


if __name__ == "__main__":
    unittest.main()
