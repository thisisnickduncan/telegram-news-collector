"""Thin wrapper over the Telegram Bot API: sendMessage, editMessageReplyMarkup,
answerCallbackQuery, getUpdates. Raw HTTP via requests, no bot framework --
matches fetcher.py's style and avoids an extra dependency for what's a handful
of simple JSON endpoints.
"""
import requests

from news_alert.config import TELEGRAM_BOT_TOKEN

API_URL = "https://api.telegram.org/bot{token}/{method}"


def safe_call(fn, *args, **kwargs):
    """Wraps a telegram_client call so a transient failure -- most commonly an
    expired callback_query id, which happens whenever the bot wasn't polling the
    instant a button was tapped -- can't take down a DB write it shouldn't be
    coupled to. Use for notification-style calls (answer/edit/send) whose failure
    the caller can tolerate; let anything the caller must know about raise normally."""
    try:
        fn(*args, **kwargs)
    except Exception as exc:
        print(f"[telegram_client] call failed (non-fatal): {exc}")


def _call(method, timeout=30, **params):
    url = API_URL.format(token=TELEGRAM_BOT_TOKEN, method=method)
    resp = requests.post(url, json=params, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error on {method}: {data}")
    return data["result"]


def send_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    """parse_mode="HTML" requires the caller to have HTML-escaped `text` (digest.py does).
    Pass parse_mode=None for arbitrary/generated text (e.g. a Claude Q&A answer) that
    hasn't been escaped -- sending it as HTML would break on a stray < > &."""
    params = {"chat_id": chat_id, "text": text}
    if parse_mode:
        params["parse_mode"] = parse_mode
    if reply_markup is not None:
        params["reply_markup"] = reply_markup
    return _call("sendMessage", **params)


def edit_message_reply_markup(chat_id, message_id, reply_markup):
    return _call(
        "editMessageReplyMarkup",
        chat_id=chat_id,
        message_id=message_id,
        reply_markup=reply_markup,
    )


def answer_callback_query(callback_query_id, text=None):
    params = {"callback_query_id": callback_query_id}
    if text:
        params["text"] = text
    return _call("answerCallbackQuery", **params)


def get_updates(offset=None, timeout=30):
    """Long-polls for new updates (messages, button presses). timeout is seconds
    Telegram holds the connection open waiting for something to happen -- kept as a
    GET with its own request timeout (not routed through _call) since that "timeout"
    is a Telegram long-poll parameter, not our HTTP client's."""
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    url = API_URL.format(token=TELEGRAM_BOT_TOKEN, method="getUpdates")
    resp = requests.get(url, params=params, timeout=timeout + 10)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error on getUpdates: {data}")
    return data["result"]
