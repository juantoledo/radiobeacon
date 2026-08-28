# Build from the REPO ROOT, not ui/ alone:
#   docker build -f ui/Dockerfile -t radiobeacon-ui .
#
# Repo-root context is required because ui/src/ui/app.py's sys.path
# bootstrap resolves REPO_ROOT via Path(__file__).parents[3] and expects
# data-adapters/src and dispatcher/src as siblings on disk — the same
# layout dispatcher/__main__.py and dispatcher/override_item.py rely on
# outside Docker. This image preserves that sibling layout under /app.
FROM python:3.12-slim
WORKDIR /app

COPY data-adapters/src /app/data-adapters/src
COPY dispatcher/src /app/dispatcher/src
COPY ui/src /app/ui/src
COPY ui/requirements.txt /app/ui/requirements.txt

RUN pip install --no-cache-dir -r /app/ui/requirements.txt

ENV PYTHONPATH=/app/ui/src
WORKDIR /app/ui
EXPOSE 8080

# storage/radiobeacon.db is not baked into the image — bind-mount it at
# runtime (see ui/docker-compose.yml or the README's `docker run` form).
# UI_HOST must be overridden to 0.0.0.0 at run time: 127.0.0.1 inside a
# container is unreachable through a port mapping from the host.
CMD ["python3", "-m", "ui"]
