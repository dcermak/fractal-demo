FROM registry.opensuse.org/opensuse/bci/python:3.14 AS base

FROM base AS build
RUN zypper -n ref; zypper -n in uv;
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
ENTRYPOINT ["/opt/fractal/.venv/bin/fractal-worker"]
CMD []
