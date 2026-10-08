FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn sqlalchemy pydantic pytest httpx pyyaml ruff bandit openapi-spec-validator
COPY . /app
CMD ["pytest", "-q"]
