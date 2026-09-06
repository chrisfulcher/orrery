# Built from source: `docker compose build` (or `docker build -t mentor .`).
# Installs exactly what uv.lock pins (--frozen); the only network fetches during the
# build are this base image and the locked wheels from PyPI.
FROM python:3.13-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PATH=/app/.venv/bin:$PATH \
    MENTOR_DATA_DIR=/data

RUN pip install --no-cache-dir 'uv==0.12.*' \
    && useradd --uid 1000 --create-home mentor \
    && mkdir /data && chown mentor:mentor /data

WORKDIR /app

# Dependencies first, so a change under src/ does not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# uv_build reads README.md (pyproject `readme`) and LICENSE when it builds the wheel.
COPY README.md LICENSE ./
COPY src/ src/
RUN uv sync --frozen --no-dev --no-editable

USER mentor
ENTRYPOINT ["mentor"]
CMD ["--help"]
