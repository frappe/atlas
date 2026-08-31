FROM python:3.12-slim
COPY upstream.py /upstream.py
ENTRYPOINT ["python3", "/upstream.py"]
