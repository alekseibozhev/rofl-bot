FROM python:3.13-slim

ARG BOT_VERSION=""
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    BOT_VERSION=${BOT_VERSION}

WORKDIR /app
COPY bot.py chat_context.py delivery.py retrieval.py ./

VOLUME ["/data"]
CMD ["python3", "-u", "bot.py"]
