FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --retries 10 --timeout 120 .

RUN useradd --create-home --uid 10001 quant \
    && mkdir -p /app/data /app/state /app/artifacts \
    && chown -R quant:quant /app/data /app/state /app/artifacts
USER quant

ENTRYPOINT ["island-quant"]
CMD ["doctor"]
