# A deliberately-broken raw-TCP backend for the L4 forwarder robustness tests
# (spec/17-tcp-proxy.md). The TCP analogue of misbehave.Dockerfile; mode chosen by
# UPSTREAM_MODE. Python stdlib only, no third-party deps.
FROM python:3.12-slim
COPY tcp_misbehave.py /tcp_misbehave.py
ENTRYPOINT ["python3", "/tcp_misbehave.py"]
