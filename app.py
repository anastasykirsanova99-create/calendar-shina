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


def get_busy_between(start_dt, end_dt):
    body = {
        "timeMin": start_dt.astimezone(timezone.utc).isoformat(),
        "timeMax": end_dt.astimezone(timezone.utc).isoformat(),
        "timeZone": TIMEZONE,
        "items": [{"id": CALENDAR_ID}]
    }

    result = service.freebusy().query(body=body).execute()
    return result["calendars"][CALENDAR_ID].get("busy", [])


def generate_free_slots(days_ahead=DAYS_AHEAD, limit=None):
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

    busy_by_date = {}
    busy_intervals = []

    for slot in busy:
        busy_start = parse_google_dt(slot["start"])
        busy_end = parse_google_dt(slot["end"])

        date_key = format_date(busy_start)

        busy_by_date.setdefault(date_key, []).append([
            format_time(busy_start),
            format_time(busy_end)
        ])

        busy_intervals.append((busy_start, busy_end))

    suggested_free_slots = []
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
            slot_start = datetime(
                day.year,
                day.month,
                day.day,
                hour,
                0,
                tzinfo=KYIV_TZ
            )

            slot_end = slot_start + timedelta(hours=SLOT_DURATION_HOURS)

            if slot_start < now:
                continue

            is_busy = False

            for busy_start, busy_end in busy_intervals:
                if slot_overlaps_busy(slot_start, slot_end, busy_start, busy_end):
                    is_busy = True
                    break

            if not is_busy:
                date_key = format_date(slot_start)
                start_time = format_time(slot_start)
                end_time = format_time(slot_end)
                slot_range = f"{start_time}-{end_time}"

                suggested_free_slots.append(f"{date_key} {start_time}")
                free_slots_by_date.setdefault(date_key, []).append(slot_range)

    if limit:
        suggested_free_slots = suggested_free_slots[:limit]

    return busy_by_date, suggested_free_slots, free_slots_by_date


@app.route('/availability', methods=['GET'])
def availability():
    try:
        current_now = datetime.now(KYIV_TZ)

        busy_by_date, suggested_free_slots, free_slots_by_date = generate_free_slots()

        response_data = {
            "success": True,
            "current_date": current_now.strftime("%d.%m.%Y"),
            "working_hours": "09:00-18:00",
            "slot_duration_minutes": 60,
            "has_busy_slots": len(busy_by_date) > 0,
            "busy_by_date": busy_by_date,
            "suggested_free_slots": suggested_free_slots,
            "free_slots_by_date": free_slots_by_date
        }

        return app.response_class(
            response=json.dumps(response_data, ensure_ascii=False),
            mimetype='application/json'
        )

    except Exception as e:
        print(traceback.format_exc())
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/availability-by-date', methods=['GET'])
def availability_by_date():
    try:
        requested_date = request.args.get("date")

        if not requested_date:
            return jsonify({
                "success": False,
                "error": "date_required",
                "message": "Потрібно передати дату у форматі dd.mm.yyyy"
            }), 400

        current_now = datetime.now(KYIV_TZ)

        busy_by_date, _, free_slots_by_date = generate_free_slots()

        free_slots = free_slots_by_date.get(requested_date, [])

        response_data = {
            "success": True,
            "current_date": current_now.strftime("%d.%m.%Y"),
            "date": requested_date,
            "available": len(free_slots) > 0,
            "free_slots": free_slots[:3],
            "busy_slots": busy_by_date.get(requested_date, []),
            "message": (
                "Є вільні слоти"
                if len(free_slots) > 0
                else "На цю дату вільного часу немає"
            )
        }

        return app.response_class(
            response=json.dumps(response_data, ensure_ascii=False),
            mimetype='application/json'
        )

    except Exception as e:
        print(traceback.format_exc())
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/create-event', methods=['POST'])
def create_event():
    try:
        data = request.json

        name = data.get('name')
        phone = data.get('phone')
        service_name = data.get('service')
        car_type = data.get('car_type')
        wheel_radius = data.get('wheel_radius')
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
                "message": "Цей час поза робочим графіком",
                "working_hours": "09:00-18:00"
            }), 409

        busy_slots = get_busy_between(start_dt, end_dt)

        if len(busy_slots) > 0:
            return jsonify({
                "success": False,
                "error": "slot_busy",
                "message": "Цей слот вже зайнятий"
            }), 409

        event = {
            'summary': f'{service_name} - {name}',
            'description': (
                f'Телефон: {phone}\n'
                f'Авто: {car_type}\n'
                f'Радіус коліс: {wheel_radius}'
            ),
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
            "message": "Appointment created",
            "date": date,
            "time": time
        })

    except Exception as e:
        print(traceback.format_exc())
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
