# A fake site VM for the proxy test harness — Python stdlib HTTP server on
# [::]:80 (proxy-design.md §9). No third-party deps.
FROM python:3.12-slim
COPY upstream.py /upstream.py
ENTRYPOINT ["python3", "/upstream.py"]
