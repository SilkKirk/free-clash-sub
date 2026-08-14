FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY crawler.py server.py ./

ENV PORT=5000 REFRESH_HOURS=6

EXPOSE 5000

CMD ["python", "server.py"]