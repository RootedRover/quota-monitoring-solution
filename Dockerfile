# One image, two workloads: the Cloud Run Job runs `collect`, the Cloud Run
# Service runs the dashboard. They share every dependency and all the model
# code, so building and scanning two images would buy nothing but drift
# between the ratio logic the collector writes and the ratio logic the
# dashboard displays.

FROM python:3.12-slim AS build

# uv resolves and installs from the committed lockfile, so the image contains
# exactly the versions CI tested and OSV-Scanner audited.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, in their own layer: they change on Dependabot's schedule,
# the application changes on ours.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project --no-dev

COPY collector/ ./collector/
COPY dashboard/ ./dashboard/
RUN uv sync --locked --no-dev


FROM python:3.12-slim

# No compiler, no uv, no build cache in the runtime image.
RUN useradd --create-home --uid 1001 qms
WORKDIR /app

COPY --from=build --chown=qms:qms /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

USER qms
EXPOSE 8080

# The Service overrides nothing; the Job overrides the command with
# `python -m collector.cli ... collect`.
CMD ["python", "-m", "dashboard.app"]
