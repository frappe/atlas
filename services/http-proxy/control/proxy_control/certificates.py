import os
import shutil
import ssl
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class _State:
    region: tuple[bytes, int] | None
    links: dict[str, str | None]
    files: dict[str, tuple[bytes, int] | None]
    target_existed: bool


class CertificateStore:
    """Validate, install, and activate the regional proxy certificate."""

    def __init__(self, directory: Path):
        self.directory = directory

    def install(self, wildcard_domain: str, fullchain_pem: str, private_key_pem: str) -> str:
        wildcard = wildcard_domain.strip().lower()
        region = self._region(wildcard)
        target = self.directory / region
        state = self._save_state(region)

        certificate = private_key = None
        try:
            target.mkdir(mode=0o750, parents=True, exist_ok=True)
            certificate = self._write_temp(target, fullchain_pem.encode(), 0o644)
            private_key = self._write_temp(target, private_key_pem.encode(), 0o640)
            self._validate_pair(certificate, private_key)
            self._validate_name(certificate, wildcard)
            self._replace(certificate, target / "fullchain.pem", 0o644)
            certificate = None
            self._replace(private_key, target / "privkey.pem", 0o640)
            private_key = None
            self._write_region(region)
            self._activate(region)
            self._reload_openresty()
        except (OSError, RuntimeError, ValueError) as error:
            self._restore_state(region, state)
            if isinstance(error, OSError):
                raise RuntimeError("cannot install proxy certificate") from error
            raise
        finally:
            for path in (certificate, private_key):
                if path:
                    Path(path).unlink(missing_ok=True)
        return region

    def _save_state(self, region: str) -> _State:
        target = self.directory / region
        region_path = self.directory.parent / "region"
        links = {
            name: os.readlink(self.directory / name)
            if (self.directory / name).is_symlink()
            else None
            for name in ("fullchain.pem", "privkey.pem")
        }
        files = {
            name: self._read_file(target / name)
            for name in ("fullchain.pem", "privkey.pem")
        }
        return _State(
            region=self._read_file(region_path),
            links=links,
            files=files,
            target_existed=target.exists(),
        )

    def _restore_state(self, region: str, state: _State) -> None:
        target = self.directory / region
        if state.target_existed:
            for name, value in state.files.items():
                path = target / name
                path.unlink(missing_ok=True)
                if value:
                    self._write_file(path, *value)
        else:
            shutil.rmtree(target, ignore_errors=True)

        for name, link_target in state.links.items():
            link = self.directory / name
            link.unlink(missing_ok=True)
            if link_target:
                link.symlink_to(link_target)

        region_path = self.directory.parent / "region"
        region_path.unlink(missing_ok=True)
        if state.region:
            self._write_file(region_path, *state.region)

    def _read_file(self, path: Path) -> tuple[bytes, int] | None:
        if not path.is_file():
            return None
        return path.read_bytes(), path.stat().st_mode & 0o777

    def _write_file(self, path: Path, content: bytes, mode: int) -> None:
        temporary = path.with_name(f".{path.name}.restore")
        temporary.write_bytes(content)
        os.chmod(temporary, mode)
        os.replace(temporary, path)

    def _region(self, wildcard: str) -> str:
        if not wildcard.startswith("*."):
            raise ValueError("wildcard_domain must start with *.")

        region = wildcard[2:]
        labels = region.split(".")
        valid = (
            region not in {"", ".", ".."}
            and "/" not in region
            and "\\" not in region
            and all(label and label[0].isalnum() and label[-1].isalnum() for label in labels)
        )
        if not valid:
            raise ValueError("wildcard_domain contains an invalid domain")
        return region

    def _write_region(self, region: str) -> None:
        path = self.directory.parent / "region"
        temporary = self.directory.parent / ".atlas-region.new"
        temporary.write_text(region + "\n")
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)

    def _write_temp(self, directory: Path, content: bytes, mode: int) -> str:
        descriptor, path = tempfile.mkstemp(prefix=".atlas-cert-", dir=directory)
        try:
            os.fchmod(descriptor, mode)
            with os.fdopen(descriptor, "wb") as file:
                file.write(content)
        except BaseException:
            Path(path).unlink(missing_ok=True)
            raise
        return path

    def _replace(self, source: str, destination: Path, mode: int) -> None:
        os.chmod(source, mode)
        os.replace(source, destination)

    def _activate(self, region: str) -> None:
        for name in ("fullchain.pem", "privkey.pem"):
            link = self.directory / name
            temporary = self.directory / f".{name}.new"
            temporary.unlink(missing_ok=True)
            temporary.symlink_to(Path(region) / name)
            os.replace(temporary, link)

    def _validate_pair(self, certificate: str, private_key: str) -> None:
        self._openssl(certificate, "x509", "-noout", "-checkend", "0")
        certificate_key = self._openssl(certificate, "x509", "-pubkey", "-noout")
        private_key_value = self._openssl(private_key, "pkey", "-pubout")
        if certificate_key != private_key_value:
            raise ValueError("certificate and private key do not match")

    def _validate_name(self, certificate: str, wildcard: str) -> None:
        try:
            decoded = ssl._ssl._test_decode_cert(certificate)
            names = {
                value.lower().rstrip(".")
                for kind, value in decoded.get("subjectAltName", [])
                if kind == "DNS"
            }
        except (OSError, ValueError) as error:
            raise ValueError("certificate does not cover wildcard_domain") from error

        if wildcard.rstrip(".") not in names:
            raise ValueError("certificate does not cover wildcard_domain")

    def _openssl(self, path: str, kind: str, *arguments: str) -> bytes:
        result = subprocess.run(
            ["openssl", kind, "-in", path, *arguments],
            capture_output=True,
            check=False,
            timeout=10,
        )
        if result.returncode != 0:
            raise ValueError(f"invalid {kind} PEM")
        return result.stdout

    def _reload_openresty(self) -> None:
        command = ["/usr/local/openresty/nginx/sbin/nginx", "-c", "/etc/nginx/nginx.conf"]
        if self._run_openresty([*command[:1], "-t", *command[1:]]) != 0:
            raise RuntimeError("OpenResty rejected the new certificate configuration")
        if self._run_openresty([*command[:1], "-s", "reload", *command[1:]]) != 0:
            raise RuntimeError("OpenResty reload failed")

    def _run_openresty(self, command: list[str]) -> int:
        return subprocess.run(command, capture_output=True, check=False, timeout=10).returncode
