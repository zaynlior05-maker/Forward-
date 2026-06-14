# Telegram Forwarder Bot

A lightweight Telegram user-bot that monitors source channels/groups and forwards or copies messages to a destination channel — with smart link-detection logic.

-----

## How It Works

Every incoming message is checked for links (URLs, `t.me/` links):

|Condition                  |Action                       |Result                                                             |
|---------------------------|-----------------------------|-------------------------------------------------------------------|
|Message **contains** a link|`forwardMessage`             |Arrives with “Forwarded from [Source]” header; links stay clickable|
|Message has **no link**    |`copyMessage` / `sendMessage`|Arrives clean with no forwarding header                            |

-----

## Setup (One-Time, Local)

### 1. Get Telegram API credentials

Go to <https://my.telegram.org/apps>, log in, and create an app.  
Copy your **API ID** and **API Hash**.

### 2. Install dependencies locally

```bash
pip install telethon python-dotenv
```

### 3. Generate your session string

```bash
python generate_session.py
```

Log in with your phone number when prompted. Copy the long `SESSION_STRING` it prints — you’ll need this for Railway.

### 4. Find your chat IDs

The easiest way: forward a message from the target channel to [@userinfobot](https://t.me/userinfobot) on Telegram. It will show the chat ID (usually a negative number like `-1001234567890`).

-----

## Deployment on Railway

1. Push this repo to a **private** GitHub repository.
1. Go to [Railway](https://railway.app) → **New Project** → **Deploy from GitHub repo**.
1. Select your repository.
1. Go to the **Variables** tab and add:

|Variable             |Value                                                         |
|---------------------|--------------------------------------------------------------|
|`TELEGRAM_API_ID`    |Your API ID                                                   |
|`TELEGRAM_API_HASH`  |Your API Hash                                                 |
|`SESSION_STRING`     |Output from `generate_session.py`                             |
|`SOURCE_CHAT_IDS`    |Comma-separated chat IDs, e.g. `-1001234567890,-1009876543210`|
|`DESTINATION_CHAT_ID`|e.g. `-1009999999999`                                         |

1. Railway will auto-detect `railway.toml` and deploy. The bot starts automatically and restarts if it crashes.

-----

## Local Development

```bash
# Clone and install
pip install -r requirements.txt

# Copy and fill in your values
cp .env.example .env
nano .env

# Run
python main.py
```

-----

## Notes

- **Session security:** The `SESSION_STRING` grants full access to your Telegram account. Never commit it to GitHub. Always add it via Railway’s environment variables panel.
- **Restricted channels:** Because this runs as a user account (not a bot), it can read from restricted/private channels your account is already a member of.
- **`cryptg` package:** Listed in `requirements.txt` as optional — it significantly speeds up media uploads/downloads via native C extensions.