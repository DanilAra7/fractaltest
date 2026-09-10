# Dockerfile для Hugging Face Space.
#
# Код не дублюється в репозиторії Space — він тягнеться з GitHub на збірці,
# тому єдине джерело правди лишається одне. Щоб підтягнути свіжий код,
# треба зробити Factory rebuild у налаштуваннях Space.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# ARG для збиття кешу шару: зміни його значення, щоб перезібрати з нуля.
ARG CACHE_BUST=1
RUN git clone --depth 1 https://github.com/DanilAra7/fractaltest.git /app

WORKDIR /app
RUN pip install --no-cache-dir -r requirements.txt

# Тека проєкту може бути недоступна на запис — кеш відповідей вішаємо на /tmp.
ENV TRIAGE_CACHE_DIR=/tmp/triage-cache
# Порт, який слухає health-сторінка; має збігатися з app_port у README Space.
ENV PORT=7860
EXPOSE 7860

CMD ["python", "-m", "src.bot"]
