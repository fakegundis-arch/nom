FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY p2pbot ./p2pbot

# Fixed uid 1000 so the ./data folder on the VPS can be given to this user
# (sudo chown 1000:1000 data) and the database is writable.
RUN useradd --create-home --uid 1000 bot && mkdir -p /app/data && chown bot /app/data
USER bot

CMD ["python", "-m", "p2pbot.bot"]
