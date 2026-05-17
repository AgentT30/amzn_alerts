# Amazon Price Alerts Bot

This project tracks Amazon product prices and sends Telegram alerts when a product price is at or below your target.

It uses:

- a Flask webhook endpoint to receive Telegram commands immediately
- SQLite to store alerts
- a separate `check` command that you can schedule with cron
- optional Docker Compose to run the webhook and checker together

The bot is intentionally single-user. It only accepts commands from the Telegram chat ID stored in `TELEGRAM_CHAT_ID`.

## Features

- Receive Telegram commands through a webhook
- Add Amazon product alerts from Telegram
- Update an existing alert instead of creating duplicates
- Store alerts in SQLite
- Soft-delete alerts instead of permanently deleting them
- Send alerts on every `check` run while the current price is below the target
- Load Telegram credentials from a local `.env` file

## Requirements

- Python 3.10+
- A Telegram bot token from BotFather
- Your Telegram chat ID
- A public HTTPS endpoint reachable by Telegram
- Docker and Docker Compose if you want containerized local or server deployment

## Installation

Create and activate a virtual environment if you want isolation:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

If you prefer `uv`, this also works:

```bash
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

## Docker Setup

This repo includes a Docker image based on `python:3.13.9-alpine` and a `docker-compose.yml` that starts both services:

- `webhook`: runs Gunicorn and serves the Telegram webhook endpoint
- `checker`: runs the hourly price check loop

Both services share the same SQLite database file through a Docker volume.

### Required `.env`

Create a `.env` file in the project root:

```env
TELEGRAM_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
TELEGRAM_WEBHOOK_SECRET=long_random_secret_string
```

### Start Everything

```bash
docker compose up --build -d
```

The webhook will be exposed on:

```text
http://localhost:8000
```

For local Telegram testing, put a tunnel such as `ngrok` or Cloudflare Tunnel in front of port `8000`, then register the HTTPS tunnel URL as your Telegram webhook.

### Stop Everything

```bash
docker compose down
```

### View Logs

```bash
docker compose logs -f webhook
docker compose logs -f checker
```

### Checker Interval

The `checker` service defaults to `3600` seconds between runs. To change it, edit the `CHECK_INTERVAL_SECONDS` value in [docker-compose.yml](/home/chaitanya-personal/Documents/amazon_alerts/docker-compose.yml).

### Persistent Data

SQLite data is stored in the named Docker volume `alerts-data`. That volume is mounted at `/app/data` inside both containers, and the database path is set to `/app/data/alerts.db`.

If you want to remove everything including the database:

```bash
docker compose down -v
```

## Environment Variables

Create a `.env` file in the project root:

```env
TELEGRAM_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
TELEGRAM_WEBHOOK_SECRET=long_random_secret_string
```

Optional:

```env
ALERTS_DB_PATH=/absolute/path/to/alerts.db
```

Notes:

- `TELEGRAM_WEBHOOK_SECRET` is used in the webhook path to make it unguessable
- if `ALERTS_DB_PATH` is not set, the script uses `alerts.db` in the project directory

## How It Works

There are two run modes:

### 1. Webhook Server

This mode runs the Flask app that receives Telegram webhook requests and processes commands immediately.

```bash
python amazon_price_check.py serve --host 127.0.0.1 --port 8000
```

Or with `uv`:

```bash
uv run python amazon_price_check.py serve --host 127.0.0.1 --port 8000
```

Routes:

- `POST /telegram/webhook/<TELEGRAM_WEBHOOK_SECRET>`
- `GET /health`

### 2. Check Active Alerts

This mode loads all active alerts from SQLite, scrapes each Amazon product, updates the latest seen price, and sends a Telegram alert if the current price is less than or equal to the saved target.

```bash
python amazon_price_check.py check
```

Or with `uv`:

```bash
uv run python amazon_price_check.py check
```

## Telegram Commands

### `/add <amazon_url> <target_price>`

Adds a new alert or updates an existing one for the same product.

Example:

```text
/add https://amzn.in/d/0acOXlvq 4000
```

What happens:

- the bot fetches the product page immediately
- extracts the product title and current price
- resolves a stable canonical Amazon product URL
- stores or updates the alert in SQLite
- sends a confirmation back to Telegram

### `/list`

Shows all active alerts.

Each entry includes:

- alert ID
- product title
- target price
- last seen price
- canonical URL

### `/remove <id>`

Soft-deletes an alert by setting `is_active = 0`.

Example:

```text
/remove 3
```

This does not permanently delete the row from the database.

### `/help`

Shows the supported commands.


## Files

- `amazon_price_check.py`: Flask webhook server and price checker
- `Dockerfile`: container image for the bot
- `docker-compose.yml`: starts the webhook and checker together
- `docker/checker.sh`: simple periodic checker loop used by the checker container
- `alerts.db`: SQLite database created automatically
- `requirements.txt`: Python dependencies
- `.env`: local Telegram configuration
