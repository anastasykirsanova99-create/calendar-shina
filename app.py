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


def normalize_text(text):
    if not text:
        return ""

    text = text.lower().strip()

    for symbol in [".", ",", "!", "?"]:
        text = text.replace(symbol, "")

    return text


def words_to_hour(text):
    text = normalize_text(text)

    mapping = {
        "дев’яти": 9, "девяти": 9, "девятої": 9, "девять": 9, "девяти": 9,
        "десяти": 10, "десятої": 10, "десять": 10,
        "одинадцяти": 11, "одинадцятої": 11, "одинадцать": 11,
        "дванадцяти": 12, "дванадцятої": 12, "дванадцать": 12,
        "часу": 13, "першої": 13, "первой": 13, "одної": 13,
        "двох": 14, "другої": 14, "двух": 14, "второй": 14,
        "трьох": 15, "третьої": 15, "трех": 15, "трёх": 15,
        "чотирьох": 16, "четвертої": 16, "четырех": 16, "четырёх": 16,
        "п’яти": 17, "пяти": 17, "п’ятої": 17, "пятої": 17,
        "шести": 18, "шостої": 18, "шесть": 18
    }

    numeric_match = re.search(r'\b([0-9]{1,2})\b', text)
    if numeric_match:
        hour = int(numeric_match.group(1))
        if 1 <= hour <= 8:
            hour += 12
        return hour

    for word, hour in mapping.items():
        if word in text:
            return hour

    return None


def extract_time_period(date_text):
    text = normalize_text(date_text)

    if any(x in text for x in ["зранку", "вранці", "ранок", "до обіду"]):
        return "morning"

    if any(x in text for x in ["після обіду", "післяобід", "вдень", "день"]):
        return "afternoon"

    if any(x in text for x in ["увечері", "ввечері", "вечером", "на вечір", "вечір", "після роботи"]):
        return "evening"

    return None


def extract_time_range(date_text):
    text = normalize_text(date_text)

    if not text:
        return None, None

    # "після двох", "после двух", "після 14"
    if "після" in text or "после" in text:
        hour = words_to_hour(text)
        if hour:
            return hour, WORK_END_HOUR

    # "з часу до трьох", "з 13 до 15", "с часу до трех"
    if ("з " in text or "с " in text) and "до" in text:
        parts = text.split("до", 1)
        start_part = parts[0]
        end_part = parts[1]

        start_hour = words_to_hour(start_part)
        end_hour = words_to_hour(end_part)

        if start_hour and end_hour:
            return start_hour, end_hour

    return None, None


def filter_slots_by_period(slots, period):
    if not period:
        return slots

    filtered = []

    for slot in slots:
        start_time = slot.split("-")[0]
        hour = int(start_time.split(":")[0])

        if period == "morning" and 9 <= hour < 12:
            filtered.append(slot)

        elif period == "afternoon" and 12 <= hour < 17:
            filtered.append(slot)

        elif period == "evening" and 17 <= hour < 18:
            filtered.append(slot)

    return filtered


def filter_slots_by_time_range(slots, start_hour, end_hour):
    if not start_hour and not end_hour:
        return slots

    filtered = []

    for slot in slots:
        slot_start = slot.split("-")[0]
        hour = int(slot_start.split(":")[0])

        if start_hour and end_hour:
            if start_hour <= hour < end_hour:
                filtered.append(slot)

        elif start_hour:
            if hour >= start_hour:
                filtered.append(slot)

    return filtered


def clean_period_and_time_words(text):
    phrases_to_remove = [
        "зранку", "вранці", "ранок", "до обіду",
        "після обіду", "післяобід", "вдень", "день",
        "увечері", "ввечері", "вечером", "на вечір",
        "вечір", "після роботи",
        "після", "после", "з", "с", "до",
        "дев’яти", "девяти", "девятої",
        "десяти", "десятої",
        "одинадцяти", "одинадцятої",
        "дванадцяти", "дванадцятої",
        "часу", "першої", "одної",
        "двох", "другої",
        "трьох", "третьої",
        "чотирьох", "четвертої",
        "п’яти", "пяти", "п’ятої", "пятої",
        "шести", "шостої"
    ]

    for phrase in phrases_to_remove:
        text = text.replace(phrase, "")

    text = re.sub(r'\b[0-9]{1,2}\b', '', text)

    return " ".join(text.split())


def resolve_date_text(date_text):
    now = datetime.now(KYIV_TZ)

    if not date_text:
        return None

    text = normalize_text(date_text)
    text = clean_period_and_time_words(text)

    if not text:
        return format_date(now)

    weekdays = {
        "понеділок": 0,
        "понеділка": 0,
        "вівторок": 1,
        "вівторка": 1,
        "середа": 2,
        "середу": 2,
        "середи": 2,
        "четвер": 3,
        "четверга": 3,
        "пятниця": 4,
        "пятницю": 4,
        "п’ятниця": 4,
        "п’ятницю": 4,
        "п'ятниця": 4,
        "п'ятницю": 4,
        "субота": 5,
        "суботу": 5,
        "неділя": 6,
        "неділю": 6,
        "неділі": 6
    }

    if text in ["сьогодні", "сегодня"]:
        return format_date(now)

    if text == "завтра":
        return format_date(now + timedelta(days=1))

    if text in ["післязавтра", "послезавтра"]:
        return format_date(now + timedelta(days=2))

    is_next = "наступ" in text or "следующ" in text

    words_to_remove = [
        "на", "у", "в", "цей", "цього", "цю",
        "будь", "ласка", "давайте", "хочу", "можна",
        "запишіть", "запис", "записатися",
        "наступний", "наступного", "наступну",
        "наступної", "наступна",
        "следующий", "следующую", "следующего", "следующая"
    ]

    parts = text.split()
    parts = [word for word in parts if word not in words_to_remove]
    text = " ".join(parts)

    if text in weekdays:
        target_weekday = weekdays[text]
        today_weekday = now.weekday()

        days_ahead = (target_weekday - today_weekday) % 7

        if days_ahead == 0:
            days_ahead = 7

        if is_next:
            days_ahead += 7

        return format_date(now + timedelta(days=days_ahead))

    digits = ''.join(ch for ch in text if ch.isdigit())

    if digits:
        day = int(digits)

        candidate = datetime(
            now.year,
            now.month,
            day,
            0,
            0,
            tzinfo=KYIV_TZ
        )

        if candidate.date() < now.date():
            if now.month == 12:
                candidate = datetime(now.year + 1, 1, day, 0, 0, tzinfo=KYIV_TZ)
            else:
                candidate = datetime(now.year, now.month + 1, day, 0, 0, tzinfo=KYIV_TZ)

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

            if is_busy:
                continue

            date_key = format_date(slot_start)
            slot_range = f"{format_time(slot_start)}-{format_time(slot_end)}"

            suggested_free_slots.append(f"{date_key} {format_time(slot_start)}")
            free_slots_by_date.setdefault(date_key, []).append(slot_range)

    return busy_by_date, suggested_free_slots, free_slots_by_date


@app.route('/availability', methods=['GET'])
def availability():
    try:
        current_now = datetime.now(KYIV_TZ)

        requested_date = request.args.get("date")
        date_text = request.args.get("date_text")

        time_period = extract_time_period(date_text)
        start_hour, end_hour = extract_time_range(date_text)

        if date_text and not requested_date:
            requested_date = resolve_date_text(date_text)

        busy_by_date, suggested_free_slots, free_slots_by_date = generate_free_slots()

        if requested_date:
            requested_dt = datetime.strptime(
                requested_date,
                "%d.%m.%Y"
            ).replace(tzinfo=KYIV_TZ)

            working_day = is_working_day(requested_dt)

            slots_for_date = (
                free_slots_by_date.get(requested_date, [])
                if working_day else []
            )

            slots_for_date = filter_slots_by_period(slots_for_date, time_period)
            slots_for_date = filter_slots_by_time_range(slots_for_date, start_hour, end_hour)

            busy_for_date = busy_by_date.get(requested_date, [])

            nearest_available_date = None
            nearest_free_slots = []

            if working_day and len(slots_for_date) == 0:
                for date_key, slots in free_slots_by_date.items():
                    filtered_slots = filter_slots_by_period(slots, time_period)
                    filtered_slots = filter_slots_by_time_range(filtered_slots, start_hour, end_hour)

                    if len(filtered_slots) > 0:
                        nearest_available_date = date_key
                        nearest_free_slots = filtered_slots[:3]
                        break

            response_data = {
                "success": True,
                "current_date": current_now.strftime("%d.%m.%Y"),
                "requested_date": requested_date,
                "working_hours": "09:00-18:00",
                "slot_duration_minutes": 60,
                "time_period": time_period,
                "start_hour": start_hour,
                "end_hour": end_hour,
                "is_working_day": working_day,
                "available": working_day and len(slots_for_date) > 0,
                "free_slots": slots_for_date[:3],
                "nearest_available_date": nearest_available_date,
                "nearest_free_slots": nearest_free_slots,
                "busy_slots": busy_for_date,
                "message": (
                    "У вихідні ми не працюємо"
                    if not working_day
                    else "Є вільні слоти"
                    if len(slots_for_date) > 0
                    else "На цю дату вільного часу немає"
                )
            }

            return app.response_class(
                response=json.dumps(response_data, ensure_ascii=False),
                mimetype='application/json'
            )

        return jsonify({
            "success": False,
            "error": "date_not_recognized",
            "message": "Не вдалося розпізнати дату"
        }), 400

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
        service_name = data.get('service')
        car_type = data.get('car_type')
        wheel_radius = data.get('wheel_radius')
        plate_number = data.get('plate_number')
        date = data.get('date')
        time = data.get('time')

        start_dt = datetime.strptime(
            f"{date} {time}",
            "%d.%m.%Y %H:%M"
        ).replace(tzinfo=KYIV_TZ)

        end_dt = start_dt + timedelta(hours=SLOT_DURATION_HOURS)

        if not is_working_day(start_dt):
            return jsonify({
                "success": False,
                "error": "non_working_day",
                "message": "У вихідні ми не працюємо"
            }), 409

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
                f'Авто: {car_type}\n'
                f'Радіус коліс: {wheel_radius}\n'
                f'Номер авто: {plate_number}'
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
