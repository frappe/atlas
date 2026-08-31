FROM python:3.12-slim
COPY misbehave.py /misbehave.py
ENTRYPOINT ["python3", "/misbehave.py"]
