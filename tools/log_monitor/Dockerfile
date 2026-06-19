FROM python:3.13-slim

# We need the docker CLI inside this container to ``docker logs -f``
# sibling containers via the mounted /var/run/docker.sock. We DON'T need
# the daemon — install only the CLI from Docker's official APT repo to
# stay current with whatever the host runs (Docker 28.x on staging).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates curl gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
       | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
       > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py /app/main.py
COPY behavioral.py /app/behavioral.py
COPY rules.yml /app/rules.yml

ENV PYTHONUNBUFFERED=1
EXPOSE 8090

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8090"]
