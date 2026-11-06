# Installing the bot on a VPS: every command

For **Ubuntu 24.04** on a VPS with a **static IP**, in a country Bybit doesn't
restrict (not the US; e.g. Frankfurt or Amsterdam). 2 vCPU, 2–4 GB RAM is plenty.

Replace these everywhere below:

| Placeholder | Example |
|---|---|
| `YOUR_VPS_IP` | `203.0.113.10` |
| `bot.yourdomain.com` | a subdomain you own, for Nomba's webhooks |

Lines starting with `#` are explanations, not commands.

---

## 1. Point a subdomain at the VPS (do this first)

At the company where you bought your domain, add a DNS record:

- Type **A**, name **bot**, value **YOUR_VPS_IP**

Nomba needs an `https://` address to send webhooks to, and Caddy (below)
gets a free HTTPS certificate for it. DNS can take a few minutes to update.

## 2. Log in to the VPS

From your own computer (Windows PowerShell, Mac or Linux Terminal):

```bash
ssh root@YOUR_VPS_IP
```

## 3. Update the server and create a normal user

```bash
apt update && apt upgrade -y
adduser deploy
usermod -aG sudo deploy
```

## 4. Set up SSH keys (so password logins can be switched off)

**On your own computer** (not the VPS), in a new terminal window:

```bash
ssh-keygen -t ed25519
# press Enter at every question

# Mac / Linux:
ssh-copy-id deploy@YOUR_VPS_IP

# Windows PowerShell (instead of ssh-copy-id):
type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh deploy@YOUR_VPS_IP "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys"
```

Then check it works **before** going further (still on your own computer):

```bash
ssh deploy@YOUR_VPS_IP
```

It should log you in without asking for the deploy password. **Only if that
worked**, turn off password and root logins (on the VPS, logged in as deploy):

```bash
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sudo systemctl restart ssh
```

From now on, always log in with `ssh deploy@YOUR_VPS_IP`.

## 5. Firewall and automatic security updates

```bash
sudo apt install -y ufw unattended-upgrades
sudo ufw allow OpenSSH
sudo ufw allow 80
sudo ufw allow 443
sudo ufw --force enable
sudo dpkg-reconfigure -f noninteractive unattended-upgrades
```

## 6. Install Docker, Caddy and git

```bash
sudo apt install -y docker.io docker-compose-v2 caddy git
sudo systemctl enable --now docker
sudo usermod -aG docker deploy
```

Log out and back in so the docker group takes effect:

```bash
exit
ssh deploy@YOUR_VPS_IP
docker ps
# should print an empty table, not "permission denied"
```

## 7. HTTPS for Nomba's webhooks (Caddy)

Replace `bot.yourdomain.com` with your subdomain, then paste the whole block:

```bash
sudo tee /etc/caddy/Caddyfile > /dev/null <<'EOF'
bot.yourdomain.com {
    reverse_proxy /webhooks/nomba 127.0.0.1:8080
    respond 404
}
EOF
sudo systemctl reload caddy
```

Only the webhook path is reachable from the internet; everything else answers 404.

## 8. Get the bot's code

```bash
cd ~
git clone https://github.com/fakegundis-arch/nom.git
cd nom
mkdir -p data
sudo chown 1000:1000 data
```

## 9. Fill in your settings

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

In nano: fill in the values, then save with **Ctrl+O**, **Enter**, and exit
with **Ctrl+X**. Start safe:

- `NOMBA_BASE_URL=https://sandbox.nomba.com` with your **sandbox** keys
- `MONITOR_ONLY=true`
- every `AUTO_...=false`

Where each value comes from:

| Setting | Where |
|---|---|
| `BYBIT_API_KEY` / `BYBIT_API_SECRET` | Bybit → API Management. Tick **FiatP2P**, restrict to **YOUR_VPS_IP**, **no withdrawal permission**. Needs P2P advertiser status. |
| `NOMBA_CLIENT_ID` / `NOMBA_CLIENT_SECRET` / `NOMBA_ACCOUNT_ID` | Nomba web dashboard → Settings → Webhook & API Keys. Use **sandbox** keys first. |
| `NOMBA_WEBHOOK_SECRET` | Same Nomba page (webhook signing key). Set Nomba's webhook URL to `https://bot.yourdomain.com/webhooks/nomba`. |
| `TELEGRAM_BOT_TOKEN` | Telegram → @BotFather → `/newbot` |
| `TELEGRAM_CHAT_ID` | Message @userinfobot; it replies with your id. Then open your new bot and press Start. |
| `BUSINESS_NAME` | Your business name exactly as on your Nomba account (needed for refills) |

Never paste these values into chats, screenshots or GitHub.

## 10. Build, then test Nomba on its own

```bash
docker compose build
docker compose run --rm bot python -m p2pbot.check
```

Every line should show ✅ (or ⏭ for skipped steps). If you see ❌, send the
output (it never prints your keys) and fix that first.

## 11. Start the bot

```bash
docker compose up -d
docker compose logs -f
```

You should get **"🤖 Bot started"** on Telegram. Press **Ctrl+C** to stop
watching the logs; the bot keeps running.

Check the webhook address works from outside:

```bash
curl -i -X POST https://bot.yourdomain.com/webhooks/nomba
# "401 Unauthorized" is CORRECT here: the bot is reachable and rejected an unsigned request.
```

## 12. Daily database backup

```bash
mkdir -p ~/backups
(crontab -l 2>/dev/null; echo '0 3 * * * cp ~/nom/data/p2pbot.sqlite3 ~/backups/p2pbot-$(date +\%F).sqlite3 && find ~/backups -mtime +30 -delete') | crontab -
```

This copies the database every night at 03:00 and keeps 30 days. Download a
copy to your own computer now and then:

```bash
# on your own computer:
scp deploy@YOUR_VPS_IP:~/backups/*.sqlite3 .
```

---

## Everyday commands

```bash
cd ~/nom
docker compose ps                      # is it running?
docker compose logs -f --tail 100      # watch what it's doing
docker compose restart                 # restart (e.g. after editing .env)
docker compose down                    # stop it completely
docker compose run --rm bot python -m p2pbot.check   # re-test Nomba
```

**Update to the latest code:**

```bash
cd ~/nom
git pull
docker compose up -d --build
```

**After changing `.env`** (e.g. switching to live Nomba keys or turning on an
`AUTO_...` switch):

```bash
docker compose up -d
```

## Going live

Follow the order in the README: sandbox → live keys in monitor-only →
`MONITOR_ONLY=false` with `AUTO_RELEASE` and small limits → `AUTO_PAY` →
`AUTO_REFILL`. `/stop` on Telegram halts every automatic action instantly.

### Optional: the refill withdraw key

Only if you want the bot to move USDT from Bybit to Nomba by itself:

1. In Nomba, find your **USDT deposit address** and its network (e.g. TRC20 / `TRX`).
2. In Bybit → Withdrawal address book, add that address and turn on
   **withdrawal whitelist** so Bybit only allows withdrawals to saved addresses.
3. Create a **second** API key with only the **Withdraw** permission,
   restricted to YOUR_VPS_IP. Put it in `BYBIT_WITHDRAW_API_KEY` /
   `BYBIT_WITHDRAW_API_SECRET`, and the address in `NOMBA_USDT_ADDRESS`.

Keep the main trading key **without** withdrawal permission.

## If a key leaks

Regenerate it immediately in Bybit or Nomba, put the new one in `.env`, and run
`docker compose up -d`.
