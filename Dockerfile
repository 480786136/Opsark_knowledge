FROM node:22-slim AS web
WORKDIR /web
COPY web/package*.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.14-slim
WORKDIR /service
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml constraints.txt ./
COPY knowledge/ knowledge/
RUN pip install --no-cache-dir -c constraints.txt .
COPY alembic.ini ./
COPY migrations/ migrations/
COPY --from=web /web/dist web/dist
RUN groupadd --gid 10001 knowledge \
    && useradd --uid 10001 --gid knowledge --no-create-home knowledge \
    && mkdir -p /service/data \
    && chown knowledge:knowledge /service/data
USER 10001:10001
CMD ["uvicorn", "knowledge.main:app", "--host", "0.0.0.0", "--port", "8002", "--workers", "1"]
