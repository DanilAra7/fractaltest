FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY data/ ./data/

# Свідомо без ENTRYPOINT, тільки CMD: образ має два режими — разовий прогін
# по CSV і довгоживучий Telegram-бот. З ENTRYPOINT команду не перевизначиш
# (Railway підставляє свою як CMD, і вона просто дописалась би в кінець).
#
# Разовий прогін (за замовчуванням, на моці — працює навіть без ключів):
#   docker run --rm -v "$PWD/out:/app/out" triage
# Реальний прогін:
#   docker run --rm --env-file .env -v "$PWD/out:/app/out" triage \
#     python -m src.main --provider gemini
# Бот:
#   docker run --rm --env-file .env triage python -m src.bot
CMD ["python", "-m", "src.main", "--provider", "mock"]
