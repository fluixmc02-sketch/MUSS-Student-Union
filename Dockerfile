FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py db.py ./
# The database lives in /data. On Railway, attach a volume at /data so it survives redeploys.
RUN mkdir -p /data
ENV DB_PATH=/data/bot.db PYTHONUNBUFFERED=1
CMD ["python", "bot.py"]
