FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
# pypdf — резюме приходят в PDF, текст надо достать.
# websockets — CDP общего браузера для сайтов за Cloudflare (Revolut).
RUN pip install --no-cache-dir pypdf==6.1.1 websockets==17.4
COPY src/ ./
CMD ["python", "bot.py"]
