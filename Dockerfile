FROM node:22-slim AS web
WORKDIR /web
COPY web/package*.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.14-slim
WORKDIR /service
COPY pyproject.toml constraints.txt ./
COPY knowledge/ knowledge/
RUN pip install --no-cache-dir -c constraints.txt .
COPY alembic.ini ./
COPY migrations/ migrations/
COPY --from=web /web/dist web/dist
CMD ["uvicorn", "knowledge.main:app", "--host", "0.0.0.0", "--port", "8002", "--workers", "1"]
