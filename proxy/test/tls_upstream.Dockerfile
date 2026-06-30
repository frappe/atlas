# A fake CUSTOM-DOMAIN site VM that terminates its OWN TLS on :443 (spec/12 § The
# stream front-door, spec/18 Phase 2) — the trust boundary the proxy's SNI passthrough
# preserves. Needs openssl (the VM self-signs its cert at startup, as a real bench VM
# does via certbot). Stdlib HTTP otherwise.
FROM python:3.12-slim
# openssl: the VM self-signs its cert at startup (as a real bench VM does via certbot).
# curl: the ACME test seeds a challenge into the VM's in-memory store via its /__seed
# control endpoint with `docker compose exec tls-vm curl ...` (stands in for certbot
# writing the webroot) — the slim base ships neither.
RUN apt-get update && apt-get install -y --no-install-recommends openssl curl && rm -rf /var/lib/apt/lists/*
COPY tls_upstream.py /tls_upstream.py
ENTRYPOINT ["python3", "/tls_upstream.py"]
