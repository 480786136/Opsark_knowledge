FROM python:3.14-slim
WORKDIR /service
COPY pyproject.toml constraints.txt ./
COPY knowledge/ knowledge/
RUN pip install --no-cache-dir -c constraints.txt .
COPY alembic.ini ./
COPY migrations/ migrations/
CMD ["uvicorn", "knowledge.main:app", "--host", "0.0.0.0", "--port", "8002", "--workers", "1"]
