import os

import uvicorn

from .config import ConfigError, load

LISTEN_PORT = 443


def main() -> None:
	try:
		config = load()
	except ConfigError as error:
		raise SystemExit(f"atlas-wg-gateway: {error}") from error

	tls_directory = config.state_directory / "tls"
	tls_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
	certificate = tls_directory / "fullchain.pem"
	key = tls_directory / "privkey.pem"
	for path, content in ((certificate, config.certificate_pem), (key, config.private_key_pem)):
		descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
		with os.fdopen(descriptor, "w") as target:
			target.write(content + "\n")

	uvicorn.run("gatewayd.main:app", host="::", port=LISTEN_PORT, ssl_certfile=certificate, ssl_keyfile=key)


if __name__ == "__main__":
	main()
