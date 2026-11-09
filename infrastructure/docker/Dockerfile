FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md alembic.ini ./
COPY src ./src
COPY alembic ./alembic
RUN pip install --no-cache-dir .

ENV APEX_ENVIRONMENT=production
ENV APEX_DEV_AUTH_ENABLED=false
ENV APEX_SEED_DEV_PRINCIPALS=false

EXPOSE 8000
USER nobody
CMD ["uvicorn", "apex.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
