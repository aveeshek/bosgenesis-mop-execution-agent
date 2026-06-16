FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

EXPOSE 8080

CMD ["uvicorn", "bosgenesis_mop_execution_agent.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
