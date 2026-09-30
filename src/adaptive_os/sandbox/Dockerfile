FROM python:3.11-slim-bookworm@sha256:a36c24f9cbdf4fd0f52d67f0823eeac19c2028c637cecc392d97f980d4fec56b
WORKDIR /opt/adaptive-os
COPY . /opt/adaptive-os/adaptive_os/sandbox/
RUN mkdir /scratch && chown 65534:65534 /scratch
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65534:65534
ENTRYPOINT ["python", "-m", "adaptive_os.sandbox.agent"]
