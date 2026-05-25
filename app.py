from flask import Flask, request, jsonify
from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import json
import os
import traceback
import re

app = Flask(__name__)

SCOPES = ['https://www.googleapis.com/auth/calendar']

info = json.loads(os.environ["GOOGLE_CREDENTIALS"])

credentials = service_account.Credentials.from_service_account_info(
    info,
    scopes=SCOPES
)

service = build('calendar', 'v3', credentials=credentials)

CALENDAR_ID = '65eb87c37a4593bf4ee2d8f63178afbb560bdbdf45e9d146447a4139f3cc681a@group.calendar.google.com'

KYIV_TZ = ZoneInfo("Europe/Kyiv")

WORK_START = 9
WORK_END = 18
SLOT_DURATION = 1


# ---------------- DATE NORMALIZATION ----------------

def normalize_date(date_str):
    today = datetime.now(KYIV_TZ).date()

    if not date_str:
        return today

    date_str = date_str.lower().strip()

    if "завтра" in date_str:
        return today + timedelta(days=1)

    if "післязавтра" in date_str:
        return today + timedelta(days=2)

    match = re.search(r"через\s*(\d+)", date_str)
    if match:
        return today + timedelta(days=int(match.group(1)))

    try:
        if len(date_str) == 5:  # 26.05
            date_str = f"{date_str}.2026"

        return datetime.strptime(date_str, "%d.%m.%Y").date()
    except:
        return today


# ---------------- TIME NORMALIZATION ----------------

def normalize_time(time_str):
    if not time_str:
        return "09:00"

    t = time_str.lower().strip()

    # "в 3", "3"
    match = re.search(r"(\d{1,2})", t)
    if match:
        hour = int(match.group(1))

        if "вечора" in t or "дня" in t or hour < 8:
            if hour != 0:
                hour += 12

        return f"{hour:02d}:00"

    if "пів на другу" in t:
        return "13:30"

    if "час дня" in t:
        return "13:00"

    return "09:00"


# ---------------- CALENDAR HELPERS ----------------

def parse_google_dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(KYIV_TZ)


def format_date(dt):
    return dt.strftime("%d.%m.%Y")


def format_time(dt):
    return dt.strftime("%H:%M")


def slot_overlaps(a_start, a_end, b_start, b_end):
    return a_start < b_end and a_end > b_start


def get_busy(start_dt, end_dt):
    body = {
        "timeMin": start_dt.astimezone(timezone.utc).isoformat(),
        "timeMax": end_dt.astimezone(timezone.utc).isoformat(),
        "timeZone": "Europe/Kyiv",
        "items": [{"id": CALENDAR_ID}]
    }

    result = service.freebusy().query(body=body).execute()
    return result["calendars"][CALENDAR_ID].get("busy", [])


# ---------------- SLOT GENERATION ----------------

def get_three_slots(date_obj):
    day_start = datetime.combine(date_obj, datetime.min.time(), tzinfo=KYIV_TZ)
    day_end = day_start + timedelta(days=1)

    busy = get_busy(day_start, day_end)

    busy_intervals = [
        (parse_google_dt(b["start"]), parse_google_dt(b["end"]))
        for b in busy
    ]

    slots = []

    for hour in range(WORK_START, WORK_END):
        start = datetime(date_obj.year, date_obj.month, date_obj.day, hour, 0, tzinfo=KYIV_TZ)
        end = start + timedelta(hours=SLOT_DURATION)

        if any(slot_overlaps(start, end, b1, b2) for b1, b2 in busy_intervals):
            continue

        slots.append(f"{format_time(start)}-{format_time(end)}")

        if len(slots) == 3:
            break

    return slots


# ---------------- CREATE EVENT ----------------

@app.route('/create-event', methods=['POST'])
def create_event():
    try:
        data = request.json

        name = data.get("name")
        phone = data.get("phone")
        service = data.get("service")

        date_obj = normalize_date(data.get("date"))
        time_str = normalize_time(data.get("time"))

        start_dt = datetime.strptime(
            f"{date_obj.strftime('%d.%m.%Y')} {time_str}",
            "%d.%m.%Y %H:%M"
        ).replace(tzinfo=KYIV_TZ)

        end_dt = start_dt + timedelta(hours=SLOT_DURATION)

        if start_dt.hour < WORK_START or end_dt.hour > WORK_END:
            return jsonify({
                "success": False,
                "error": "outside_working_hours"
            }), 409

        busy = get_busy(start_dt, end_dt)

        if busy:
            return jsonify({
                "success": False,
                "error": "slot_busy"
            }), 409

        event = {
            "summary": f"{service} - {name}",
            "description": f"Phone: {phone}",
            "start": {"dateTime": start_dt.isoformat(), "timeZone": "Europe/Kyiv"},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": "Europe/Kyiv"},
        }

        service.events().insert(calendarId=CALENDAR_ID, body=event).execute()

        return jsonify({
            "success": True,
            "date": format_date(start_dt),
            "time": f"{format_time(start_dt)}-{format_time(end_dt)}"
        })

    except Exception as e:
        print(traceback.format_exc())
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# ---------------- AVAILABILITY ----------------

@app.route('/availability', methods=['POST'])
def availability():
    try:
        data = request.json or {}

        date_obj = normalize_date(data.get("date"))
        slots = get_three_slots(date_obj)

        return jsonify({
            "current_date": format_date(datetime.now(KYIV_TZ)),
            "date": format_date(datetime.combine(date_obj, datetime.min.time())),
            "slots": slots
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
