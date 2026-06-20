# A deliberately-broken site VM for the proxy robustness tests — a raw-socket
# server that replies with non-HTTP garbage / truncated bodies on [::]:80. Python
# stdlib only, no third-party deps. See misbehave.py for the failure modes.
FROM python:3.12-slim
COPY misbehave.py /misbehave.py
ENTRYPOINT ["python3", "/misbehave.py"]
