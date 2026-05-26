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

    d = str(date_str).lower().strip()

    if "завтра" in d:
        return today + timedelta(days=1)

    if "післязавтра" in d:
        return today + timedelta(days=2)

    match = re.search(r"через\s*(\d+)", d)
    if match:
        return today + timedelta(days=int(match.group(1)))

    try:
        if len(d) == 5:
            d = f"{d}.2026"

        return datetime.strptime(d, "%d.%m.%Y").date()

    except:
        return today


# ---------------- TIME NORMALIZATION ----------------

def normalize_time(time_str):
    if not time_str:
        return "09:00"

    t = str(time_str).lower().strip()

    # 14:00-15:00 -> 14:00
    if "-" in t:
        t = t.split("-")[0].strip()

    if "час дня" in t:
        return "13:00"

    if "пів на другу" in t:
        return "13:30"

    match = re.search(r"(\d{1,2})", t)

    if match:
        hour = int(match.group(1))

        if "дня" in t or "вечора" in t:
            if hour < 12:
                hour += 12

        return f"{hour:02d}:00"

    return "09:00"


# ---------------- HELPERS ----------------

def parse_google_dt(value):
    return datetime.fromisoformat(
        value.replace("Z", "+00:00")
    ).astimezone(KYIV_TZ)


def format_date(dt):
    return dt.strftime("%d.%m.%Y")


def format_time(dt):
    return dt.strftime("%H:%M")


def overlaps(a1, a2, b1, b2):
    return a1 < b2 and a2 > b1


def get_busy(start_dt, end_dt):
    body = {
        "timeMin": start_dt.astimezone(timezone.utc).isoformat(),
        "timeMax": end_dt.astimezone(timezone.utc).isoformat(),
        "timeZone": "Europe/Kyiv",
        "items": [{"id": CALENDAR_ID}]
    }

    result = service.freebusy().query(body=body).execute()

    return result["calendars"][CALENDAR_ID].get("busy", [])


# ---------------- AVAILABILITY ----------------

@app.route('/availability', methods=['GET', 'POST'])
def availability():
    try:
        data = request.get_json(silent=True) or {}

        date_raw = (
            request.args.get("date")
            or data.get("date")
        )

        date_obj = normalize_date(date_raw)

        day_start = datetime.combine(
            date_obj,
            datetime.min.time(),
            tzinfo=KYIV_TZ
        )

        day_end = day_start + timedelta(days=1)

        busy = get_busy(day_start, day_end)

        busy_intervals = []
        busy_by_date = {}

        for item in busy:
            busy_start = parse_google_dt(item["start"])
            busy_end = parse_google_dt(item["end"])

            busy_intervals.append((busy_start, busy_end))

            date_key = format_date(busy_start)

            busy_by_date.setdefault(date_key, []).append([
                format_time(busy_start),
                format_time(busy_end)
            ])

        suggested_free_slots = []

        for hour in range(WORK_START, WORK_END):
            start = datetime(
                date_obj.year,
                date_obj.month,
                date_obj.day,
                hour,
                0,
                tzinfo=KYIV_TZ
            )

            end = start + timedelta(hours=SLOT_DURATION)

            if any(
                overlaps(start, end, b1, b2)
                for b1, b2 in busy_intervals
            ):
                continue

            suggested_free_slots.append(
                f"{format_date(start)} {format_time(start)}"
            )

            if len(suggested_free_slots) == 3:
                break

        response_data = {
            "success": True,

            "current_date": format_date(
                datetime.now(KYIV_TZ)
            ),

            "working_hours": "09:00-18:00",

            "slot_duration_minutes": 60,

            "has_busy_slots": len(busy_by_date) > 0,

            "busy_by_date": busy_by_date,

            "suggested_free_slots": suggested_free_slots
        }

        return app.response_class(
            response=json.dumps(
                response_data,
                ensure_ascii=False
            ),
            mimetype='application/json'
        )

    except Exception as e:
        print(traceback.format_exc())

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# ---------------- CREATE EVENT ----------------

@app.route('/create-event', methods=['POST'])
def create_event():
    try:
        data = request.get_json(silent=True) or {}

        name = data.get("name")
        phone = data.get("phone")
        service_name = data.get("service")

        date_obj = normalize_date(data.get("date"))
        time_str = normalize_time(data.get("time"))

        start_dt = datetime.strptime(
            f"{date_obj.strftime('%d.%m.%Y')} {time_str}",
            "%d.%m.%Y %H:%M"
        ).replace(tzinfo=KYIV_TZ)

        end_dt = start_dt + timedelta(hours=SLOT_DURATION)

        # Проверка рабочих часов
        if start_dt.hour < WORK_START or end_dt.hour > WORK_END:
            return jsonify({
                "success": False,
                "error": "outside_working_hours",
                "working_hours": "09:00-18:00"
            }), 409

        # Проверка занятости
        busy = get_busy(start_dt, end_dt)

        if busy:
            return jsonify({
                "success": False,
                "error": "slot_busy"
            }), 409

        # Создание события
        event = {
            "summary": f"{service_name} - {name}",

            "description": f"Телефон: {phone}",

            "start": {
                "dateTime": start_dt.isoformat(),
                "timeZone": "Europe/Kyiv"
            },

            "end": {
                "dateTime": end_dt.isoformat(),
                "timeZone": "Europe/Kyiv"
            }
        }

        service.events().insert(
            calendarId=CALENDAR_ID,
            body=event
        ).execute()

        return jsonify({
            "success": True,
            "date": format_date(start_dt),
            "time": format_time(start_dt)
        })

    except Exception as e:
        print(traceback.format_exc())

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
