FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
# pypdf — единственная зависимость: резюме приходят в PDF, текст надо достать
RUN pip install --no-cache-dir pypdf==6.1.1
COPY src/ ./
CMD ["python", "bot.py"]
