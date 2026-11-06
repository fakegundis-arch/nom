# Deploying on a Linux VPS

Ubuntu 24.04, 2 vCPU, 2–4 GB RAM, **static IP**, in a country Bybit doesn't
restrict (not the US; a European location such as Frankfurt is usually fine).

You also need a domain or subdomain (for example `bot.yourname.com`) pointing
at the VPS, because Nomba must send webhooks over HTTPS.

## 1. Secure the server

```bash
# as root, once
adduser deploy && usermod -aG sudo deploy
# copy your SSH key to the new user, then in /etc/ssh/sshd_config set:
#   PasswordAuthentication no
#   PermitRootLogin no
systemctl restart ssh

apt update && apt install -y ufw unattended-upgrades
ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw enable
dpkg-reconfigure -plow unattended-upgrades
```

## 2. Install Docker and Caddy

```bash
apt install -y docker.io docker-compose-v2 caddy
usermod -aG docker deploy
```

Put this in `/etc/caddy/Caddyfile` (use your domain), then `systemctl reload caddy`:

```
bot.yourname.com {
    reverse_proxy /webhooks/nomba 127.0.0.1:8080
    respond 404
}
```

Caddy gets the HTTPS certificate automatically. Only the webhook path is exposed.

## 3. Get the code and settings

```bash
git clone https://github.com/fakegundis-arch/nom.git && cd nom
cp .env.example .env
nano .env        # fill in the keys HERE, on the server only
chmod 600 .env
```

Where each value comes from:

| Setting | Where |
|---|---|
| `BYBIT_API_KEY` / `BYBIT_API_SECRET` | Bybit → API Management. Tick **FiatP2P**, restrict to the VPS IP, **no withdrawal permission**. Needs P2P advertiser status. |
| `NOMBA_CLIENT_ID` / `NOMBA_CLIENT_SECRET` / `NOMBA_ACCOUNT_ID` | Nomba web dashboard → Settings → Webhook & API Keys. Use **sandbox** keys first. |
| `NOMBA_WEBHOOK_SECRET` | Same Nomba page (webhook signing key). Set the webhook URL there to `https://bot.yourname.com/webhooks/nomba`. |
| `TELEGRAM_BOT_TOKEN` | Telegram → @BotFather → `/newbot` |
| `TELEGRAM_CHAT_ID` | Send any message to @userinfobot; it replies with your id. Then send `/start` to your new bot. |

## 4. Start it

```bash
docker compose up -d --build
docker compose logs -f          # watch it start; you should get "🤖 Bot started" on Telegram
```

Update later with `git pull && docker compose up -d --build`.

## 5. Backups

The database is in `nom/data/`. Copy it off the server daily, for example:

```bash
# crontab -e
0 3 * * * cp ~/nom/data/p2pbot.sqlite3 ~/backup-$(date +\%F).sqlite3
```

and download those copies regularly.

## If a key leaks

Regenerate it immediately in Bybit or Nomba, update `.env`, and run
`docker compose up -d`.
