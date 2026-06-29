# A fake CUSTOM-DOMAIN site VM that terminates its OWN TLS on :443 (spec/12 § The
# stream front-door, spec/18 Phase 2) — the trust boundary the proxy's SNI passthrough
# preserves. Needs openssl (the VM self-signs its cert at startup, as a real bench VM
# does via certbot). Stdlib HTTP otherwise.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends openssl && rm -rf /var/lib/apt/lists/*
COPY tls_upstream.py /tls_upstream.py
ENTRYPOINT ["python3", "/tls_upstream.py"]
