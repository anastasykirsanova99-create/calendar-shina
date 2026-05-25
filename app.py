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


# ---------------- UTILS ----------------

def parse_google_dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(KYIV_TZ)


def format_date(dt):
    return dt.strftime("%d.%m.%Y")


def format_time(dt):
    return dt.strftime("%H:%M")


def slot_overlaps(slot_start, slot_end, busy_start, busy_end):
    return slot_start < busy_end and slot_end > busy_start


# ---------------- GOOGLE BUSY ----------------

def get_busy_slots(start_dt, end_dt):
    body = {
        "timeMin": start_dt.astimezone(timezone.utc).isoformat(),
        "timeMax": end_dt.astimezone(timezone.utc).isoformat(),
        "timeZone": TIMEZONE,
        "items": [{"id": CALENDAR_ID}]
    }

    result = service.freebusy().query(body=body).execute()
    return result["calendars"][CALENDAR_ID].get("busy", [])


# ---------------- SLOT GENERATION (UPDATED) ----------------

def get_three_slots_for_date(date_str):
    """
    Возвращает 3 свободных слота в формате:
    11:00-12:00
    """

    day = datetime.strptime(date_str, "%d.%m.%Y").date()

    day_start = datetime.combine(day, datetime.min.time(), tzinfo=KYIV_TZ)
    day_end = day_start + timedelta(days=1)

    busy = get_busy_slots(day_start, day_end)

    busy_intervals = []
    for b in busy:
        busy_intervals.append((
            parse_google_dt(b["start"]),
            parse_google_dt(b["end"])
        ))

    free_slots = []

    for hour in range(WORK_START_HOUR, WORK_END_HOUR):
        slot_start = datetime(day.year, day.month, day.day, hour, 0, tzinfo=KYIV_TZ)
        slot_end = slot_start + timedelta(hours=SLOT_DURATION_HOURS)

        is_busy = False

        for b_start, b_end in busy_intervals:
            if slot_overlaps(slot_start, slot_end, b_start, b_end):
                is_busy = True
                break

        if not is_busy:
            free_slots.append(
                f"{format_time(slot_start)}-{format_time(slot_end)}"
            )

        if len(free_slots) == 3:
            break

    return free_slots


# ---------------- CREATE EVENT ----------------

@app.route('/create-event', methods=['POST'])
def create_event():
    try:
        data = request.json

        name = data.get('name')
        phone = data.get('phone')
        service_name = data.get('service')
        date = data.get('date')
        time = data.get('time')

        start_dt = datetime.strptime(
            f"{date} {time}",
            "%d.%m.%Y %H:%M"
        ).replace(tzinfo=KYIV_TZ)

        end_dt = start_dt + timedelta(hours=SLOT_DURATION_HOURS)

        if start_dt.hour < WORK_START_HOUR or end_dt.hour > WORK_END_HOUR:
            return jsonify({
                "success": False,
                "error": "outside_working_hours",
                "working_hours": "09:00-18:00"
            }), 409

        busy = get_busy_slots(start_dt, end_dt)

        if len(busy) > 0:
            return jsonify({
                "success": False,
                "error": "slot_busy"
            }), 409

        event = {
            'summary': f'{service_name} - {name}',
            'description': f'Телефон: {phone}',
            'start': {
                'dateTime': start_dt.isoformat(),
                'timeZone': TIMEZONE,
            },
            'end': {
                'dateTime': end_dt.isoformat(),
                'timeZone': TIMEZONE,
            },
        }

        service.events().insert(
            calendarId=CALENDAR_ID,
            body=event
        ).execute()

        return jsonify({
            "success": True,
            "date": date,
            "time": time
        })

    except Exception as e:
        print(traceback.format_exc())
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# ---------------- AVAILABILITY ----------------

@app.route('/availability', methods=['GET'])
def availability():
    try:
        current_date = datetime.now(KYIV_TZ).strftime("%d.%m.%Y")

        slots = get_three_slots_for_date(current_date)

        return jsonify({
            "current_date": current_date,
            "slots": slots
        })

    except Exception as e:
        print(traceback.format_exc())
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
