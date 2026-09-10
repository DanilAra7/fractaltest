FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY data/ ./data/

# За замовчуванням — мок: образ запускається і без ключа.
# Реальний прогін: docker run --env-file .env -v "$PWD/out:/app/out" triage --provider gemini
ENTRYPOINT ["python", "-m", "src.main"]
CMD ["--provider", "mock"]
