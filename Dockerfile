FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
# pypdf — единственная зависимость: резюме приходят в PDF, текст надо достать
RUN pip install --no-cache-dir pypdf==6.1.1
COPY bot.py llm.py jobs.py storage.py ./
CMD ["python", "bot.py"]
