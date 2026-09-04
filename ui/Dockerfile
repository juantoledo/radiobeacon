# Build from the REPO ROOT, not ui/ alone:
#   docker build -f ui/Dockerfile -t radiobeacon-ui .
#
# Repo-root context is required because ui/ depends on the sibling
# data-adapters/ and dispatcher/ packages (installed editable from
# ui/requirements.txt) and ui/src/ui/beacon_audio.py resolves REPO_ROOT via
# Path(__file__).parents[3]. This image preserves that sibling layout under
# /app.
FROM python:3.12-slim
WORKDIR /app

# pyproject.toml + src for each editable dependency, then ui itself.
COPY data-adapters/pyproject.toml /app/data-adapters/pyproject.toml
COPY data-adapters/src /app/data-adapters/src
COPY dispatcher/pyproject.toml /app/dispatcher/pyproject.toml
COPY dispatcher/src /app/dispatcher/src
COPY ui/src /app/ui/src
COPY ui/requirements.txt /app/ui/requirements.txt

# cd into ui/ so the `-e ../data-adapters` / `-e ../dispatcher` lines in
# ui/requirements.txt resolve to the siblings copied above.
WORKDIR /app/ui
RUN pip install --no-cache-dir -r requirements.txt

ENV PYTHONPATH=/app/ui/src
EXPOSE 8080

# storage/radiobeacon.db is not baked into the image — bind-mount it at
# runtime (see ui/docker-compose.yml or the README's `docker run` form).
# UI_HOST defaults to 0.0.0.0 and UI_ALLOWED_HOSTS to "*", so the app is
# reachable through a port mapping with no host env vars; the `ports:`/`-p`
# binding is what controls host-side reach.
CMD ["python3", "-m", "ui"]
