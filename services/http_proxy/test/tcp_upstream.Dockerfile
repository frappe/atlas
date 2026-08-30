# A fake raw-TCP service VM for the proxy test harness — Python stdlib TCP echo
# server on [::]:7000 (spec/17-tcp-proxy.md). No third-party deps; mirrors the
# HTTP upstream.Dockerfile.
FROM python:3.12-slim
COPY tcp_upstream.py /tcp_upstream.py
ENTRYPOINT ["python3", "/tcp_upstream.py"]
