from flask import Flask, request, jsonify, redirect
from flask_cors import CORS
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pymongo import MongoClient
import traceback
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
import base64, os
from dotenv import load_dotenv

load_dotenv(dotenv_path="Mailhelper\.env")

app = Flask(__name__)
CORS(app)


SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
EMAIL_ADDRESS = "priyamtomar133@gmail.com"
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD_SMTP", "")


MONGO_URI = os.environ.get("MONGO_URI")
DATABASE_NAME = "Camp"
COLLECTION_NAME = "Maildata"
client = MongoClient(MONGO_URI)
db = client[DATABASE_NAME]
collection = db[COLLECTION_NAME]


def authenticate_gmail(token_file, credentials_file="credentials.json"):
    creds = None
    SCOPES = [
        "https://www.googleapis.com/auth/drive.metadata.readonly",
        "https://www.googleapis.com/auth/gmail.send",
    ]
    token_file = f"{token_file}.json"
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file)
        if not creds or not creds.has_scopes(SCOPES):
            print(
                "Existing token does not have the required scopes. Re-authenticating..."
            )
            creds = None
        else:
            print(creds.scopes)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_file, "w") as token:
            token.write(creds.to_json())

    return creds


def send_mails_helper_gcp(
    idx,
    emails,
    subject,
    body_template,
    track,
    token_file,
    results,
):
    try:

        creds = authenticate_gmail(token_file.replace("@", "").replace(".com", ""))
        service = build("gmail", "v1", credentials=creds)

        for email_data in emails:
            try:

                try:
                    to_email = email_data["email"]
                    variables = email_data.get("variables", {})
                except Exception:
                    to_email = email_data
                    variables = {"name": "Test"}
                url = "http://15.207.71.80"

                if track:
                    tracking_pixel_url = f"{url}/track.png?name={to_email}&idx={idx}"
                    variables["tracking_pixel_url"] = tracking_pixel_url
                    body_template_plain = (
                        body_template
                        + """
                        <p>This email is tracked by MailSend API.</p>
                        <img src="{tracking_pixel_url}" alt="." width="1">
                        """
                    )
                    body = body_template_plain.format(**variables)
                else:
                    body = body_template.format(**variables)

                msg = MIMEMultipart()
                msg["From"] = "me"
                msg["To"] = to_email
                msg["Subject"] = subject

                html_body = MIMEText(body, "html")
                msg.attach(html_body)

                raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

                message = {"raw": raw}
                service.users().messages().send(userId="me", body=message).execute()
                print("Mail Sent Successfully")
                results.append({"email": to_email, "status": "sent"})
            except HttpError as e:

                results.append({"email": to_email, "status": "failed", "error": str(e)})
            except Exception as e:
                results.append({"email": to_email, "status": "failed", "error": str(e)})

    except Exception as e:
        print(f"An error occurred during Gmail API setup: {e}")
        results.append({"email": to_email, "status": "failed", "error": str(e)})
    return results


def schedule():
    today = datetime.now().date()
    documents = collection.find({"target": {"$exists": True, "$ne": None}})
    result = []
    for doc in documents:
        if doc["target"].date() == today and doc["status"] != "Completed":
            emails = doc["emails"]
            subject = f"Follow Up Mail Campaign {doc['uploadId']}"
            body = "Follow up body Mail to {name}"
            tracking = True
            send_mails_helper_gcp(
                doc["uploadId"],
                emails,
                subject,
                body,
                tracking,
                f'{doc["sender"].replace("@", "").replace(".com", "")}',
                result,
            )
            if doc["status"][0] == "R":
                newStatus = "1 follow up"
            else:
                newStatus = str(int(doc["status"][0]) + 1) + " follow up"
                if newStatus[0] == "4":
                    newStatus = "Completed"
            collection.update_one(
                {"_id": doc["_id"]},
                {
                    "$set": {
                        "target": ["none"],
                        "status": newStatus,
                        "target": datetime.now() + timedelta(days=3),
                    }
                },
            )
    if result:
        return jsonify({"status": "success", "result": result})
    return jsonify({"status": "No pending campaigns"})


@app.route("/schedule", methods=["GET"])
def schedule_job():
    try:
        return schedule()
    except Exception as e:
        print(e)
        return jsonify({"status": "Checking Failed"}), 500


@app.route("/authenticate", methods=["POST"])
def authenticate():
    data = request.json

    token_file = data.get("token_file_name")

    if not token_file:
        return jsonify({"status": "error", "message": "Missing required fields"}), 400

    try:
        authenticate_gmail(token_file.replace("@", "").replace(".com", ""))
    except Exception as e:
        print(e)

    return jsonify({"status": f"{token_file} has been created"}), 200


@app.route("/list_sheets", methods=["GET"])
def list_google_sheets():
    sender = request.args.get("sender")
    try:
        cred = authenticate_gmail(sender.replace("@", "").replace(".com", ""))
    except Exception as e:
        print(e)
        return
    service = build("drive", "v3", credentials=cred)
    results = (
        service.files()
        .list(
            q="mimeType='application/vnd.google-apps.spreadsheet'",
            fields="nextPageToken, files(id, name)",
        )
        .execute()
    )
    sheets = results.get("files", [])
    if not sheets:
        print("No Google Sheets files found.")
    else:
        result = []
        for sheet in sheets:
            result.append(f"{sheet['name']} ({sheet['id']})")
    return jsonify({"status": "success", "result": result}), 200


@app.route("/send-mails", methods=["POST"])
def send_mails():
    try:
        data = request.json
        sender = data.get("sender")
        idx = data.get("uploadId", 0)
        subject = data.get("subject")
        body_template = data.get("body")
        track = data.get("tracking")
        emails = data.get("emails", [])

        if not sender or not subject or not body_template or not emails:
            return (
                jsonify({"status": "error", "message": "Missing required fields"}),
                400,
            )
        results = []
        try:

            send_mails_helper_gcp(
                idx,
                emails,
                subject,
                body_template,
                track,
                f'{sender.replace("@", "").replace(".com", "")}',
                results,
            )

        except Exception:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": f"Token {sender.replace('@','')}.json file not found",
                    }
                ),
                500,
            )
        return jsonify({"status": "success", "results": results}), 200

    except Exception as e:
        return (
            jsonify(
                {
                    "status": "error",
                    "message": str(e),
                    "traceback": traceback.format_exc(),
                }
            ),
            500,
        )


@app.route("/upload", methods=["POST"])
def upload_to_mongodb():
    data = request.json
    try:
        if "date" in data:
            data["date"] = datetime.now()
        if "followup" in data and data["followup"] != 0:
            data["target"] = datetime.now() + timedelta(days=int(data["followup"]))
        result = collection.insert_one(data)
        return jsonify({"status": "success", "inserted_id": str(result.inserted_id)})
    except Exception as e:
        return jsonify({"status": "failed", "error": str(e)})


@app.route("/latest_id", methods=["GET"])
def get_latest_id():
    latest_document = collection.find_one(sort=[("uploadId", -1)])
    if latest_document:
        return jsonify({"Latest_id": latest_document.get("uploadId", 0)})
    else:
        return jsonify({"Latest_id": 0})


@app.route("/track", methods=["GET"])
def track_url():
    url = request.args.get("url")
    idx = request.args.get("id")

    if url and idx:
        clean_url = url.strip('"')

        collection.update_one(
            {"uploadId": int(idx)},
            {"$set": {"access_log": {"timestamp": datetime.now(), "url": clean_url}}},
        )
        print(idx)
        return redirect(clean_url)
    else:
        return "Invalid parameters", 400


@app.route("/timeSchedule", methods=["GET"])
def timeSchedule():
    hours = int(request.args.get("hours"))
    minutes = int(request.args.get("minute"))
    try:
        scheduler = BackgroundScheduler()
        scheduler.start()

        scheduler.add_job(
            func=schedule,
            trigger=CronTrigger(hour=hours, minute=minutes),
            id=f"Job_fixing_{hours}_{minutes}",
            replace_existing=True,
        )
    except Exception:
        pass


if __name__ == "__main__":
    app.run(debug=True)
