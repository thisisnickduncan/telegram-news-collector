"""Long-polls Telegram and dispatches updates: callback_query -> callback_handler,
message -> message_handler. Also runs the fetch/dedupe/bias/summarize/send pipeline
on a 4-hour cron schedule, clocked to Pacific time (00:00/04:00/08:00/12:00/16:00/20:00
on the dot, DST-aware), in the same persistent process. Plan section 5.9.

Uses APScheduler's thread-based BackgroundScheduler rather than AsyncIOScheduler --
telegram_client is synchronous (plain requests, matching fetcher.py's style), and a
background thread for the cron job needs no async rewrite of the rest of the bot.
"""
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import news_alert.pipeline as pipeline
from news_alert.bias import refresh_bias_data
from news_alert.callback_handler import handle_callback_query
from news_alert.config import DISPLAY_TIMEZONE
from news_alert.db import db_cursor, get_preferences
from news_alert.message_handler import handle_message
from news_alert.telegram_client import get_updates, safe_call, send_message

# Same zone the digest timestamps use -- keeping these in sync matters: a digest
# scheduled in one zone and labeled in another is how "8:00pm" became "3:26am".
SCHEDULE_TIMEZONE = DISPLAY_TIMEZONE

# When digests should ARRIVE (Pacific, DST-aware) -- not when the job starts.
SEND_HOURS = (0, 4, 8, 12, 16, 20)

# How early to start so the digest can still land exactly on the hour. The fetch is
# the variable part: GDELT answers in ~12s whether it says yes or no, and 429s enough
# that a query can need all 7 attempts, so a run measured anywhere from ~1 to ~13
# minutes. 15 covers the measured worst case with margin; the finished digest then
# waits out the remainder (see pipeline._hold_until), making arrival exact regardless
# of which way the fetch went. Must stay under 60 -- _pipeline_trigger() assumes the
# job fires in the hour before the send hour.
LEAD_MINUTES = 15

BIAS_REFRESH_DAY = 1        # 1st of each month
BIAS_REFRESH_HOUR = 3       # 3am Pacific -- clear of the 4-hour digest slots (0/4/8/12/16/20)


def _alert_failure(context, exc_info_printed=True):
    """Shared failure path for both scheduled jobs (plan section 5.8) -- a silently
    broken cron job is worse than no system at all."""
    if exc_info_printed:
        traceback.print_exc()
    prefs = get_preferences()
    if prefs and prefs.get("telegram_chat_id"):
        safe_call(send_message, prefs["telegram_chat_id"],
                  f"news-alert {context} failed, check logs.", parse_mode=None)


def _pipeline_trigger():
    """Fires LEAD_MINUTES before each send hour, so the run has time to finish."""
    if not 0 < LEAD_MINUTES < 60:
        raise ValueError(f"LEAD_MINUTES must be between 1 and 59, got {LEAD_MINUTES}")
    hours = ",".join(str((h - 1) % 24) for h in sorted(SEND_HOURS))
    return CronTrigger(hour=hours, minute=60 - LEAD_MINUTES, timezone=SCHEDULE_TIMEZONE)


def _target_send_time(now):
    """The send slot this run is working toward -- the top of the upcoming hour.

    Derived by adding the lead to `now` rather than searching SEND_HOURS, so it stays
    correct when a misfire makes the job start late: a run that starts at 19:50
    instead of 19:45 still targets 20:00, and pipeline sends immediately if that has
    already passed by the time the digest is ready.
    """
    return (now + timedelta(minutes=LEAD_MINUTES)).replace(minute=0, second=0, microsecond=0)


def run_scheduled_pipeline():
    send_at = _target_send_time(datetime.now(ZoneInfo(SCHEDULE_TIMEZONE)))
    print(f"[bot_runner] scheduled pipeline run starting, targeting {send_at:%Y-%m-%d %H:%M:%S %Z}...")
    try:
        pipeline.run(send_at=send_at)
        print("[bot_runner] scheduled pipeline run finished.")
    except Exception:
        print("[bot_runner] scheduled pipeline run FAILED:")
        _alert_failure("pipeline run")


def run_bias_refresh():
    print("[bot_runner] monthly bias-data refresh starting...")
    try:
        with db_cursor() as cur:
            seeded, missing = refresh_bias_data(cur)
        print(f"[bot_runner] bias refresh finished: seeded {seeded}, {len(missing)} outlet(s) not found"
              f"{f' ({missing})' if missing else ''}.")
    except Exception:
        print("[bot_runner] monthly bias-data refresh FAILED:")
        _alert_failure("bias-data refresh")


def start_scheduler():
    scheduler = BackgroundScheduler(timezone=SCHEDULE_TIMEZONE)
    # misfire_grace_time: if the process was briefly down (restart, deploy) right at
    # the scheduled minute, still fire if we come back within a reasonable window rather
    # than silently skipping that cycle entirely.
    scheduler.add_job(run_scheduled_pipeline, _pipeline_trigger(), id="pipeline_4h",
                      misfire_grace_time=300)

    bias_trigger = CronTrigger(day=BIAS_REFRESH_DAY, hour=BIAS_REFRESH_HOUR, minute=0,
                                timezone=SCHEDULE_TIMEZONE)
    scheduler.add_job(run_bias_refresh, bias_trigger, id="bias_refresh_monthly", misfire_grace_time=3600)

    scheduler.start()
    # The pipeline job's next_run_time is the LEAD_MINUTES-early start, not the delivery
    # time, so print both -- otherwise the log looks like the schedule has slipped.
    pipeline_job = scheduler.get_job("pipeline_4h")
    print(f"[bot_runner] scheduler started: pipeline_4h next run: {pipeline_job.next_run_time} "
          f"(delivers {_target_send_time(pipeline_job.next_run_time):%H:%M:%S %Z}, "
          f"{LEAD_MINUTES}min lead)")
    bias_job = scheduler.get_job("bias_refresh_monthly")
    print(f"[bot_runner] scheduler started: bias_refresh_monthly next run: {bias_job.next_run_time}")
    return scheduler


def _update_chat_id(update):
    """The chat an update arrived from, whichever kind of update it is."""
    if "callback_query" in update:
        container = (update.get("callback_query") or {}).get("message") or {}
    else:
        container = update.get("message") or {}
    return (container.get("chat") or {}).get("id")


def _update_actor_id(update):
    """The user who sent the message or pressed the button -- logged on a rejection so
    a repeated prober is identifiable; not used for the decision itself."""
    source = update.get("callback_query") or update.get("message") or {}
    return (source.get("from") or {}).get("id")


def _authorized(update):
    """Whether an update came from the one chat this bot belongs to.

    A Telegram bot is a public endpoint -- anyone who finds its @name can message it,
    and every handler past this point acts as though the sender were the owner: a plain
    message can reach the Sonnet + web-search path with no ceiling on cost, permanently
    append to the tracked topics, or queue deep dives that steer the owner's next
    digest. So the poller drops foreign updates before dispatching anything.

    Fails closed on purpose. If no chat id is registered there is no owner to compare
    against, so nothing is authorized -- run scripts/register_bot.py to set one. That
    script does its own polling and is unaffected by this check.
    """
    prefs = get_preferences()
    owner = (prefs or {}).get("telegram_chat_id")
    chat_id = _update_chat_id(update)

    # Telegram sends ids as ints and the column is TEXT -- compared raw, every single
    # update would look unauthorized.
    if owner and chat_id is not None and str(owner).strip() == str(chat_id):
        return True

    print(f"[bot_runner] DROPPED update {update.get('update_id')} from unauthorized "
          f"chat_id={chat_id!r} user_id={_update_actor_id(update)!r} "
          f"(owner chat id {'is not set' if not owner else 'does not match'})")
    return False


def run(max_iterations=None):
    """max_iterations bounds the loop for manual testing; leave None for the real
    always-on process (systemd will restart it if it ever exits)."""
    offset = None
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        updates = get_updates(offset=offset, timeout=30)
        for update in updates:
            offset = update["update_id"] + 1
            # Before the raw dump below: an unauthorized update's contents don't belong
            # in the log either.
            if not _authorized(update):
                continue
            print(f"[bot_runner] update {update['update_id']}: keys={list(update.keys())} raw={update}")
            try:
                if "callback_query" in update:
                    with db_cursor() as cur:
                        handle_callback_query(cur, update["callback_query"])
                elif "message" in update:
                    with db_cursor() as cur:
                        handle_message(cur, update["message"])
                else:
                    print(f"[bot_runner] update {update['update_id']} has no callback_query or message -- ignoring")
            except Exception:
                print(f"[bot_runner] error handling update {update.get('update_id')}:")
                traceback.print_exc()
        iterations += 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    start_scheduler()
    print("[bot_runner] Polling for updates (Ctrl+C to stop)...")
    run()
