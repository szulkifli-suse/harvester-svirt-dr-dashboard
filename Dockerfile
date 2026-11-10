FROM registry.suse.com/bci/python:3.13

ARG VERSION=dev
LABEL org.opencontainers.image.title="harvester-dashboard" \
      org.opencontainers.image.description="Monitor and power-manage a nested SUSE Virtualization (Harvester) lab on AWS" \
      org.opencontainers.image.version="${VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/deps \
    HOME=/tmp

WORKDIR /app
COPY requirements.txt .
RUN python3.13 -m pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore \
      --target /opt/deps -r requirements.txt
COPY app ./app
RUN python3.13 -c "import app.main, app.ops, app.status"

USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["python3.13", "-m", "uvicorn", "--factory", "app.main:create_app", \
            "--host=0.0.0.0", "--port=8080", "--proxy-headers", "--forwarded-allow-ips=*", "--no-server-header"]
