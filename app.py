from flask import Flask, request
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)

SCOPES = ['https://www.googleapis.com/auth/calendar']

import json
import os

info = json.loads(os.environ["GOOGLE_CREDENTIALS"])

credentials = service_account.Credentials.from_service_account_info(
    info,
    scopes=SCOPES
)

service = build('calendar', 'v3', credentials=credentials)

CALENDAR_ID = 'primary'


@app.route('/create-event', methods=['POST'])
def create_event():
    data = request.json

    name = data.get('name')
    phone = data.get('phone')
    service_name = data.get('service')
    date = data.get('date')
    time = data.get('time')

    event = {
        'summary': f'{service_name} - {name}',
        'description': f'Телефон: {phone}',
        'start': {
            'dateTime': f'{date}T{time}:00',
            'timeZone': 'Europe/Kyiv',
        },
        'end': {
            'dateTime': f'{date}T{time}:00',
            'timeZone': 'Europe/Kyiv',
        },
    }

    service.events().insert(
        calendarId=CALENDAR_ID,
        body=event
    ).execute()

    return {"success": True}


if __name__ == '__main__':
    app.run(port=5000)
