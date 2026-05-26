from flask import Flask, request, jsonify
from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import json
import os
import traceback

app = Flask(__name__)

SCOPES = ['https://www.googleapis.com/auth/calendar']

info = json.loads(os.environ["GOOGLE_CREDENTIALS"])

credentials = service_account.Credentials.from_service_account_info(
    info,
    scopes=SCOPES
)

service = build('calendar', 'v3', credentials=credentials)

CALENDAR_ID = '65eb87c37a4593bf4ee2d8f63178afbb560bdbdf45e9d146447a4139f3cc681a@group.calendar.google.com'

TIMEZONE = 'Europe/Kyiv'
KYIV_TZ = ZoneInfo(TIMEZONE)

WORK_START_HOUR = 9
WORK_END_HOUR = 18
SLOT_DURATION_HOURS = 1
DAYS_AHEAD = 5


def parse_google_dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(KYIV_TZ)


def format_date(dt):
    return dt.strftime("%d.%m.%Y")


def format_time(dt):
    return dt.strftime("%H:%M")


def is_working_day(dt):
    return dt.weekday() < 5


def slot_overlaps_busy(slot_start, slot_end, busy_start, busy_end):
    return slot_start < busy_end and slot_end > busy_start


def normalize_date_text(text):
    text = text.lower().strip()

    for symbol in [".", ",", "!", "?"]:
        text = text.replace(symbol, "")

    filler_words = [
        "на ", "у ", "в ", "цей ", "цього ", "цю ",
        "будь ласка ", "давайте ", "хочу ", "можна ",
        "запишіть ", "записатися "
    ]

    for word in filler_words:
        text = text.replace(word, "")

    return " ".join(text.split())


def resolve_date_text(date_text):
    now = datetime.now(KYIV_TZ)

    if not date_text:
        return None

    text = normalize_date_text(date_text)

    weekdays = {
        "понеділок": 0, "понеділка": 0,
        "вівторок": 1, "вівторка": 1,
        "середа": 2, "середу": 2,
        "четвер": 3, "четверга": 3,
        "п’ятниця": 4, "п'ятниця": 4,
        "субота": 5, "суботу": 5,
        "неділя": 6, "неділю": 6
    }

    if text in ["сьогодні"]:
        return format_date(now)

    if text in ["завтра"]:
        return format_date(now + timedelta(days=1))

    if text in ["післязавтра"]:
        return format_date(now + timedelta(days=2))

    # 🔥 ИСПРАВЛЕННЫЙ БЛОК
    if text in weekdays:
        target_weekday = weekdays[text]
        today_weekday = now.weekday()

        days_ahead = (target_weekday - today_weekday) % 7

        if days_ahead == 0:
            days_ahead = 7

        return format_date(now + timedelta(days=days_ahead))

    digits = ''.join(ch for ch in text if ch.isdigit())

    if digits:
        day = int(digits)

        candidate = datetime(now.year, now.month, day, tzinfo=KYIV_TZ)

        if candidate.date() < now.date():
            if now.month == 12:
                candidate = datetime(now.year + 1, 1, day, tzinfo=KYIV_TZ)
            else:
                candidate = datetime(now.year, now.month + 1, day, tzinfo=KYIV_TZ)

        return format_date(candidate)

    return None


def get_busy_between(start_dt, end_dt):
    body = {
        "timeMin": start_dt.astimezone(timezone.utc).isoformat(),
        "timeMax": end_dt.astimezone(timezone.utc).isoformat(),
        "timeZone": TIMEZONE,
        "items": [{"id": CALENDAR_ID}]
    }

    result = service.freebusy().query(body=body).execute()
    return result["calendars"][CALENDAR_ID].get("busy", [])


def generate_free_slots(days_ahead=DAYS_AHEAD):
    now = datetime.now(KYIV_TZ)
    search_end = now + timedelta(days=10)

    body = {
        "timeMin": now.astimezone(timezone.utc).isoformat(),
        "timeMax": search_end.astimezone(timezone.utc).isoformat(),
        "timeZone": TIMEZONE,
        "items": [{"id": CALENDAR_ID}]
    }

    result = service.freebusy().query(body=body).execute()
    busy = result["calendars"][CALENDAR_ID].get("busy", [])

    busy_intervals = []

    for slot in busy:
        busy_start = parse_google_dt(slot["start"])
        busy_end = parse_google_dt(slot["end"])
        busy_intervals.append((busy_start, busy_end))

    free_slots_by_date = {}

    current_day = now.date()
    checked_days = 0
    day_offset = 0

    while checked_days < days_ahead:
        day = current_day + timedelta(days=day_offset)
        day_dt = datetime.combine(day, datetime.min.time(), tzinfo=KYIV_TZ)

        day_offset += 1

        if not is_working_day(day_dt):
            continue

        checked_days += 1

        for hour in range(WORK_START_HOUR, WORK_END_HOUR):
            slot_start = datetime(day.year, day.month, day.day, hour, tzinfo=KYIV_TZ)
            slot_end = slot_start + timedelta(hours=1)

            if slot_start < now:
                continue

            if any(slot_overlaps_busy(slot_start, slot_end, b_start, b_end)
                   for b_start, b_end in busy_intervals):
                continue

            date_key = format_date(slot_start)
            slot_range = f"{format_time(slot_start)}-{format_time(slot_end)}"

            free_slots_by_date.setdefault(date_key, []).append(slot_range)

    return free_slots_by_date


@app.route('/availability', methods=['GET'])
def availability():
    try:
        current_now = datetime.now(KYIV_TZ)

        date_text = request.args.get("date_text")
        requested_date = resolve_date_text(date_text)

        free_slots_by_date = generate_free_slots()

        requested_dt = datetime.strptime(requested_date, "%d.%m.%Y").replace(tzinfo=KYIV_TZ)
        working_day = is_working_day(requested_dt)

        slots = free_slots_by_date.get(requested_date, []) if working_day else []

        return jsonify({
            "success": True,
            "current_date": current_now.strftime("%d.%m.%Y"),
            "requested_date": requested_date,
            "is_working_day": working_day,
            "available": working_day and len(slots) > 0,
            "free_slots": slots[:3]
        })

    except Exception as e:
        print(traceback.format_exc())
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
