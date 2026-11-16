# ICAI Nashik BatchWatch — Telegram-connected monitoring website

**Status:** Website and Telegram integration code are ready for deployment. The 11 automated tests run locally, but the live ICAI dropdown/result flow has **not** been verified from this development environment. The website is **not hosted yet** and it has **not sent any live Telegram messages**.

The monitor watches the [ICAI Launch Batch Details](https://www.icaionlineregistration.org/launchbatchdetail.aspx) page for **Western Region → POU Nashik → all selectable courses**. It checks automatically approximately every **5 minutes after the previous check finishes**, stores the observed batches, and sends Telegram alerts for previously unseen batches. The first successful scan sets a baseline; existing batches do not trigger false new alerts.

## 1. Secure the bot first

The user created **[@PranjalAlert_bot](https://t.me/PranjalAlert_bot)**, but subsequently pasted its bot access token into a chat. **Revoke the last shared token again** via Telegram **@BotFather → `/revoke` → @PranjalAlert_bot**. Do not put a bot token in code, a ZIP, a GitHub commit, a screenshot, or a message. This project intentionally contains **no live bot token**.

The destination `8855842471` supplied earlier might be a *phone number*, not necessarily a Telegram **Chat ID**. Telegram Bot API requires a Chat ID and the recipient must send `/start` to your bot. The dashboard includes **Find my Telegram chats**, **Verify & save**, and **Send another test message** so you can select the correct destination safely. It stores only the verified recipient Chat ID in the persistent SQLite database; the bot token remains in the host's secret environment settings.

## 2. Deploy the website to Railway (recommended)

1. Download the ZIP, extract it, and create a **private GitHub repository**. Upload the extracted files at the repository root (`app.py`, `monitor.py`, `storage.py`, `static/`, `Dockerfile`, etc.). **Never commit `.env` or a live bot token.**
2. Sign in to [Railway](https://railway.com/), select **New Project → Deploy from GitHub repo**, and choose this repository. Railway can build with the included `Dockerfile` and `railway.json`.
3. In your Railway service **Variables**, add:

   | Name | Value |
   | --- | --- |
   | `DASHBOARD_PASSWORD` | Your unique password, at least 12 characters (16+ recommended) |
   | `SESSION_SECRET` | A different random 32+ character secret |
   | `TELEGRAM_BOT_TOKEN` | The **fresh token** from BotFather (never paste in this chat) |
   | `POLL_MINUTES` | `5` |
   | `COOKIE_SECURE` | `true` |
   | `DATA_DIR` | `/data` |
   | `DEBUG_DIR` | `/data/debug` |

   `TELEGRAM_CHAT_ID` is **optional**. If you know and have confirmed it, you can set it here. Otherwise choose the recipient from the dashboard after deployment. You can generate `SESSION_SECRET` on your computer with `python -c "import secrets; print(secrets.token_urlsafe(40))"`.
4. Attach a **persistent Railway Volume** mounted at `/data` *before* relying on the watcher. Otherwise a restart may lose batch history, causing missed/duplicate alerts. Railway's [volume guide](https://docs.railway.com/volumes) explains this process. Use **one service replica / one web worker** to avoid parallel schedulers.
5. Generate a public HTTPS domain under **Service Settings → Networking → Generate Domain**. Open your domain and log in with `DASHBOARD_PASSWORD`.
6. Open **Settings → Telegram connection**, then:
   - Ask the intended recipient to tap **Start** on [@PranjalAlert_bot](https://t.me/PranjalAlert_bot) and send `Hello`.
   - Click **Find my Telegram chats**. Select the correct private conversation.
   - Click **Verify & save**. The system sends a real fixed test message. **Only if Telegram confirms delivery** will it persist that Chat ID for future alerts.
   - If you are certain `8855842471` is an actual Telegram Chat ID, you may enter it manually and click Verify & save. If it is only a phone number, this will fail. Prefer the chat-discovery approach.
7. Open the **Overview** page. Click **Check for new batches** to test the ICAI checker. The dashboard displays the last success, failures and batch history. A first successful run saves the baseline. Future new batches trigger messages automatically.

**Deployment costs/availability:** The monitor uses a real Chromium browser and SQLite, so it requires a continuously running server with persistent storage. Railway may have usage charges depending on your plan. A static-site host on its own cannot perform 24/7 background checks, and free services that sleep cannot guarantee this schedule.

## 3. Run locally on your own computer

Install Python 3.12+:

```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS / Linux:
# source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

Copy `.env.example` to `.env`, replace the dashboard password and session secret, insert the **new** token and leave `TELEGRAM_CHAT_ID` blank if you want to select the recipient in the dashboard. Then:

```bash
uvicorn app:app --host 127.0.0.1 --port 8000
```

Visit `http://127.0.0.1:8000`. **Set `COOKIE_SECURE=false` only for local HTTP**, not for production HTTPS.

Using Docker locally is also supported: after creating `.env`, run `docker compose up --build -d`, then open `http://localhost:8000`.

## 4. Dashboard features

- Password-protected overview and status of monitoring for Western Region/Nashik/all courses
- Manual **Check for new batches** control and automatic checks
- Full observed batch history and activity/errors log
- Telegram bot connection and recent private-chat discovery
- Telegram verification **before** saving recipient Chat ID
- Test-message button, queued alert retries, and deduplication
- Browser-based ASP.NET dropdown navigation via Playwright
- Persistent SQLite store and a ready-to-deploy Docker container

The app uses only the official [ICAI launch-batch page](https://www.icaionlineregistration.org/launchbatchdetail.aspx) as the batch data source. It is an independent tool, not affiliated with ICAI. If the official website changes markup, blocks automation, or requires a CAPTCHA/login, the monitor may stop working and will show errors. It does not bypass access controls.

**Failure policy:** If the parser cannot recognize a results grid or an explicit empty-results message, it marks the check as failed and saves debug screenshots/HTML to `/data/debug/`. It does not silently replace batch history with an empty result. Notifications for newly discovered batches are queued and retried after transient Telegram delivery failures, although rare network acknowledgment problems could produce duplicate notifications.

## 5. Telegram setup troubleshooting

| Problem | What to do |
| --- | --- |
| Bot not configured | Set the rotated `TELEGRAM_BOT_TOKEN` in Railway Variables, then redeploy |
| No conversations found | Recipient must message the bot; send `/start` and `Hello`, then try again |
| GetUpdates says webhook active | Disable the bot's existing webhook in Telegram if you intentionally configured one |
| Verify & save fails | Check the chat ID is a Telegram Chat ID, recipient has started the bot, and token is valid |
| ICAI checks fail | Read the Activity Log and inspect `/data/debug` files; dropdowns/results may have changed |
| Monitor stops | Check Railway usage limits, Playwright memory, single replica, persistent volume, and service uptime |

## 6. Tests

`python -m pytest -q` (11 local tests, using simulated batch data and mocked Telegram calls). They cover batch parsing, baseline/deduplication, persistent alerts, login/CSRF, chat discovery, recipient validation, and non-persistence of failed recipient tests. **These do not constitute a successful live ICAI or Telegram end-to-end test.**

### Files

- `app.py`: password-protected FastAPI dashboard and scheduler + Telegram configuration routes
- `monitor.py`: browser-based ICAI scanner, parser, Telegram API, formatters
- `storage.py`: SQLite persistent batch history and verified recipient setting
- `static/`: HTML/CSS/JS responsive dashboard
- `Dockerfile`, `railway.json`, `docker-compose.yml`: hosting / local container setup
- `.env.example`: secret variable names with placeholders only
