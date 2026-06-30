# PayPilot — production image
# Build:  docker build -t paypilot .
# Run:    docker run -p 8000:8000 --env-file .env paypilot
FROM python:3.11-slim

# Keep Python lean and predictable inside the container.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first so this layer is cached across code-only changes.
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy the application code and its data (playbook + customer fixtures).
COPY app/ ./app/
COPY data/ ./data/

EXPOSE 8000

# Serve the FastAPI app. OPENAI_API_KEY is supplied at runtime via --env-file.
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
