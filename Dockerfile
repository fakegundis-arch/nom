FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY p2pbot ./p2pbot

RUN useradd --create-home bot && mkdir -p /app/data && chown bot /app/data
USER bot

CMD ["python", "-m", "p2pbot.bot"]
