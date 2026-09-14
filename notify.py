"""
Telegram notifications.

Nothing here is allowed to break the thing it reports on: a notification that
fails is logged and forgotten, never raised into the job that triggered it.
"""
import requests

import config
from store import log

TIMEOUT = 15


def configured():
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


def send(text):
    """Returns None on success, or a reason why it did not go out."""
    if not configured():
        return "no bot token or chat id set"
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, timeout=TIMEOUT, json={
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
    except requests.RequestException as e:
        return f"could not reach Telegram: {e}"
    if r.status_code != 200:
        # Telegram explains itself well; pass its own wording through
        try:
            return r.json().get("description") or r.text[:160]
        except ValueError:
            return f"HTTP {r.status_code}: {r.text[:160]}"
    return None


def _emit(text):
    problem = send(text)
    if problem:
        log(f"Telegram notification not sent: {problem}")


def job_failed(kind, item_id, title, message):
    if config.NOTIFY_ON == "off":
        return
    _emit(f"⚠️ <b>Vinted reposter</b>\n{kind} of <b>{title or item_id}</b> failed:\n"
          f"<code>{message[:400]}</code>")


def repost_done(item_id, title, new_id, price_note=""):
    if config.NOTIFY_ON != "all":
        return
    _emit(f"✅ <b>Vinted reposter</b>\nReposted <b>{title}</b>{price_note}\n"
          f"#{item_id} → #{new_id}")


def token_problem(message):
    if config.NOTIFY_ON == "off":
        return
    _emit(f"🔑 <b>Vinted reposter</b>\nThe login needs attention:\n"
          f"<code>{message[:400]}</code>")
