FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
RUN python -m pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 quant
USER quant

ENTRYPOINT ["island-quant"]
CMD ["doctor"]

