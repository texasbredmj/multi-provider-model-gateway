FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml requirements.lock ./
COPY src ./src
RUN pip install --no-cache-dir -r requirements.lock . && useradd --create-home gateway
USER gateway
ENTRYPOINT ["python", "-m", "uvicorn"]
CMD ["model_gateway.app:configured_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
