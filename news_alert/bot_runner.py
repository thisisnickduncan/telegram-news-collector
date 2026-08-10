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
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import news_alert.pipeline as pipeline
from news_alert.bias import refresh_bias_data
from news_alert.callback_handler import handle_callback_query
from news_alert.db import db_cursor, get_preferences
from news_alert.message_handler import handle_message
from news_alert.telegram_client import get_updates, safe_call, send_message

SCHEDULE_TIMEZONE = "America/Los_Angeles"
SCHEDULE_HOURS = "0,4,8,12,16,20"  # every 4 hours, on the dot, Pacific time (DST-aware)
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


def run_scheduled_pipeline():
    print("[bot_runner] scheduled pipeline run starting...")
    try:
        pipeline.run()
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
    pipeline_trigger = CronTrigger(hour=SCHEDULE_HOURS, minute=0, timezone=SCHEDULE_TIMEZONE)
    scheduler.add_job(run_scheduled_pipeline, pipeline_trigger, id="pipeline_4h", misfire_grace_time=300)

    bias_trigger = CronTrigger(day=BIAS_REFRESH_DAY, hour=BIAS_REFRESH_HOUR, minute=0,
                                timezone=SCHEDULE_TIMEZONE)
    scheduler.add_job(run_bias_refresh, bias_trigger, id="bias_refresh_monthly", misfire_grace_time=3600)

    scheduler.start()
    for job_id in ("pipeline_4h", "bias_refresh_monthly"):
        job = scheduler.get_job(job_id)
        print(f"[bot_runner] scheduler started: {job_id} next run: {job.next_run_time}")
    return scheduler


def run(max_iterations=None):
    """max_iterations bounds the loop for manual testing; leave None for the real
    always-on process (systemd will restart it if it ever exits)."""
    offset = None
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        updates = get_updates(offset=offset, timeout=30)
        for update in updates:
            offset = update["update_id"] + 1
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
