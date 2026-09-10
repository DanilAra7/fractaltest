FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY data/ ./data/
# Готові результати прогону: бот віддає їх у чат як output.json і report.md.
COPY out/ ./out/

# Свідомо без ENTRYPOINT, тільки CMD: образ має два режими — довгоживучий
# Telegram-бот і разовий прогін по CSV. З ENTRYPOINT команду не перевизначиш.
#
# За замовчуванням — бот, бо саме заради нього образ і деплоїться. Спершу
# дефолтом був разовий прогін, і на першому ж деплої це вистрелило: хостинг
# зібрав коміт без railway.json, запустив CMD, той чесно відпрацював за
# півсекунди й завершився — контейнер погас без жодної помилки в логах.
# Тепер образ робить корисне навіть тоді, коли команду ніхто не задав.
#
# Бот (за замовчуванням, потрібні ключі):
#   docker run --rm --env-file .env triage
# Разовий прогін по CSV:
#   docker run --rm -v "$PWD/out:/app/out" triage \
#     python -m src.main --provider mock
CMD ["python", "-m", "src.bot"]
