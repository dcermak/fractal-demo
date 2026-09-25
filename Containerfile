FROM registry.opensuse.org/opensuse/bci/python:3.14 AS base

FROM base AS build
RUN zypper -n ref && zypper -n in uv
ENV UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/fractal/.venv \
    UV_LINK_MODE=copy
WORKDIR /build
COPY pyproject.toml uv.lock .python-version ./
COPY src/ ./src/
RUN uv sync --frozen --no-dev --no-editable --no-cache --python python3.14

FROM base AS runtime
ENV PATH="/opt/fractal/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
COPY --from=build /opt/fractal/.venv /opt/fractal/.venv
WORKDIR /opt/fractal
USER 10001:10001
EXPOSE 8080
STOPSIGNAL SIGTERM

FROM runtime AS gateway
USER 0:0
RUN zypper -n ref && zypper -n in --no-recommends helm && zypper clean -a
USER 10001:10001
ENV HOME=/tmp
COPY deploy/helm/fractal-demo/ /opt/fractal/deploy/helm/fractal-demo/
ENTRYPOINT ["/opt/fractal/.venv/bin/fractal-gateway"]
CMD []

# Keep the worker as the default target for existing build commands.
FROM runtime AS worker
ENTRYPOINT ["/opt/fractal/.venv/bin/fractal-worker"]
CMD []
