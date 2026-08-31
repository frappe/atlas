FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends openssl curl && rm -rf /var/lib/apt/lists/*
COPY tls_upstream.py /tls_upstream.py
ENTRYPOINT ["python3", "/tls_upstream.py"]
