from flask import Flask, request, jsonify, redirect
from flask_cors import CORS
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
from time import sleep
from dotenv import load_dotenv
from apscheduler.triggers.date import DateTrigger

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
        "https://www.googleapis.com/auth/spreadsheets.readonly",
    ]
    token_file = f"{token_file}.json"
    print("token_file", token_file)
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
    max_emails=0,
    delay=0,
    followup=False,
    thread_data=None,
):
    try:
        url = "http://15.207.71.80"
        creds = authenticate_gmail(token_file.replace("@", "").replace(".com", ""))
        service = build("gmail", "v1", credentials=creds)
        email_count = 0
        mail_data = []
        for email_data in emails:
            if max_emails != 0 and email_count >= max_emails:
                break
            try:

                try:
                    to_email = email_data["email"]
                    variables = email_data.get("variables", {})
                except Exception:
                    to_email = email_data
                    variables = {"name": "Test"}

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
                if "userID=#" in body:
                    body = body.replace("userID=#", f"userID={idx}")
                if "Email=#" in body:
                    body = body.replace("Email=#", f"Email={to_email}")
                msg = MIMEMultipart()
                msg["From"] = "me"
                msg["To"] = to_email
                msg["Subject"] = subject

                html_body = MIMEText(body, "html")
                msg.attach(html_body)

                raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
                collection.find_one(sort=[("uploadId", -1)])
                message = {"raw": raw}

                # For Scheduled mails or follow-ups

                if followup and thread_data:
                    thread_id = next(
                        (
                            data.split(", ")[1]
                            for data in thread_data
                            if data.split(", ")[0] == to_email
                        ),
                        None,
                    )
                    new_message = {
                        "raw": raw,
                        "threadId": thread_id,
                    }

                    response = (
                        service.users()
                        .messages()
                        .send(userId="me", body=new_message)
                        .execute()
                    )
                else:
                    response = (
                        service.users()
                        .messages()
                        .send(userId="me", body=message)
                        .execute()
                    )
                thread_idx = response["threadId"]
                print("Mail Sent. Thread ID:", thread_idx)
                results.append({"email": to_email, "status": "sent"})
            except HttpError as e:

                results.append({"email": to_email, "status": "failed", "error": str(e)})
            except Exception as e:
                results.append({"email": to_email, "status": "failed", "error": str(e)})
            mail_data.append(f"{to_email}, {thread_idx}")
            email_count += 1
            if delay > 0:
                if delay == 1:
                    sleep(7)
                elif delay == 2:
                    sleep(20)
                elif delay == 3:
                    sleep(120)
                elif delay == 4:
                    sleep(300)
            print("Uploading thread ID to the database", thread_idx)
        if thread_idx:
            collection.update_one(
                {"uploadId": idx},
                {"$set": {"thread_data": mail_data, "last_sent": datetime.now()}},
            )

    except Exception as e:
        print(f"An error occurred during Gmail API setup: {e}")
        results.append({"email": to_email, "status": "failed", "error": str(e)})
    return results


def schedule():
    collection = db[COLLECTION_NAME]
    today = datetime.now().date()
    documents = collection.find(
        {
            "schedule": {"$exists": True, "$ne": None},
        }
    )
    result = []

    for doc in documents:
        if doc["schedule"] != "Executed" and doc["schedule"] != "":
            print(
                doc["schedule"].time() <= datetime.now().time(),
                doc["schedule"],
                datetime.now().time(),
            )
        if (
            doc["schedule"] != "Executed"
            and doc["schedule"] != ""
            and doc["schedule"].date() == today
            and doc["schedule"].hour == datetime.now().hour
            and doc["schedule"].minute <= datetime.now().minute
        ):
            emails = doc["emails"]
            subject = doc["subject"]
            body = doc["body"]
            tracking = doc["tracking"]
            delay = doc["DelayCheckbox"]
            send_mails_helper_gcp(
                doc["uploadId"],
                emails,
                subject,
                body,
                tracking,
                f'{doc["sender"].replace("@", "").replace(".com", "")}',
                result,
                delay=delay,
            )
            if doc["status"][0] == "R":
                newStatus = "1 follow up"
            else:
                newStatus = str(int(doc["status"][0]) + 1) + " follow up"
                if newStatus[0] == "4":
                    newStatus = "Completed"
            collection.update_one(
                {"_id": doc["_id"]},
                {"$set": {"status": newStatus, "schedule": "Executed"}},
            )


def followUpSchedule():
    collection = db[COLLECTION_NAME]
    today = datetime.now().date()
    documents = collection.find(
        {
            "$or": [
                {"stage1": {"$exists": True, "$ne": None}},
                {"stage2": {"$exists": True, "$ne": None}},
                {"stage3": {"$exists": True, "$ne": None}},
            ]
        }
    )

    result = []
    for doc in documents:
        for i, stage in enumerate(["stage1", "stage2", "stage3"]):
            if (
                stage in doc
                and doc[stage] != False
                and doc[stage] != "Executed"
                and doc["status"] != "Completed"
                and doc[stage].date() == today
            ):
                processFollowUp(doc, i, stage, result)

    return result


def processFollowUp(doc, i, stage, result):
    print(f"Processing {stage} for document ID {doc['_id']} on {datetime.now().date()}")

    emails = doc["emails"]
    if i < len(doc["stageData"]):
        subject = doc["stageData"][i] + " For " + doc["subject"]
    else:
        subject = f"Followup For {doc['subject']}"
    body = "Follow up body Mail to " + doc["body"]
    tracking = doc["tracking"]
    try:
        thread_data_idx = doc["thread_data"]
    except Exception:
        thread_data_idx = None
    send_mails_helper_gcp(
        doc["uploadId"],
        emails,
        subject,
        body,
        tracking,
        f'{doc["sender"].replace("@", "").replace(".com", "")}',
        result,
        thread_data=thread_data_idx,
        followup=True,
    )
    if doc["status"][0] == "R":
        newStatus = "1 follow up"
    else:
        newStatus = str(int(doc["status"][0]) + 1) + " follow up"
        if newStatus[0] == "4":
            newStatus = "Completed"
    collection.update_one(
        {"_id": doc["_id"]},
        {"$set": {"status": newStatus, stage: "Executed"}},
    )


def timeSchedule(year=None, month=None, day=None, hours=None, minutes=None):
    try:
        now = datetime.now()

        year = year or now.year
        month = month or now.month
        day = day or now.day
        hours = hours if hours is not None else now.hour
        minutes = minutes if minutes is not None else now.minute

        # Initialize scheduler and define the trigger
        scheduler = BackgroundScheduler()
        scheduler.start()

        trigger = DateTrigger(run_date=datetime(year, month, day, hours, minutes))

        scheduler.add_job(
            func=schedule,
            trigger=trigger,
            id=f"Job_fixing_{year}_{month}_{day}_{hours}_{minutes}",
            replace_existing=True,
        )
        print(f"Job scheduled for {year}-{month}-{day} at {hours}:{minutes}.")
    except Exception as e:
        print(f"An error occurred while scheduling: {e}")


@app.route("/schedule", methods=["GET"])
def schedule_job():
    try:
        schedule()
    except Exception as e:
        print(e)
        return jsonify({"status": "Checking Failed"}), 500
    return "success", 200


@app.route("/followUpschedule", methods=["GET"])
def follow_job():
    try:
        followUpSchedule()
    except Exception as e:
        print(e)
        return jsonify({"status": "Checking Failed"}), 500
    return "success", 200


@app.route("/timeSchedule", methods=["GET"])
def timeSchedule_job():
    hours = int(request.args.get("hours"))
    minutes = int(request.args.get("minute"))
    timeSchedule(hours, minutes)
    return "success", 200


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


@app.route("/list-sheets", methods=["GET"])
def list_google_sheets():
    sender = request.args.get("sender")
    try:
        cred = authenticate_gmail(sender.replace("@", "").replace(".com", ""))
    except Exception as e:
        print(e)
        return jsonify({"error": "Authentication failed"}), 403
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


@app.route("/sheet-data", methods=["GET"])
def get_sheet_data():
    sender = request.args.get("sender")
    spreadsheet_id = request.args.get("spreadsheetId")
    range_name = request.args.get("range")

    try:
        cred = authenticate_gmail(sender.replace("@", "").replace(".com", ""))
    except Exception as e:
        print(f"Authentication error: {e}")
        return jsonify({"error": "Authentication failed"}), 403

    try:
        service = build("sheets", "v4", credentials=cred)
        sheet = service.spreadsheets()
        result = (
            sheet.values().get(spreadsheetId=spreadsheet_id, range=range_name).execute()
        )
        rows = result.get("values", [])
    except Exception as e:
        print(f"An error occurred while fetching data: {e}")
        return jsonify({"error": "Failed to fetch sheet data"}), 500

    if not rows or not isinstance(rows, list) or len(rows) < 1:
        print("No data found or invalid sheet structure.")
        return jsonify({"error": "No data found"}), 400

    headers = rows[0]
    if not any("email" in header.lower() for header in headers):
        print("No 'Email' column found in the sheet.")
        return jsonify({"error": "No 'Email' column in the sheet"}), 400

    return jsonify({"values": rows}), 200


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
        delay = data.get("DelayCheckbox", 0)
        existing_subject = collection.find_one({"subject": subject})
        if existing_subject:
            idx = existing_subject.get("uploadId", 0)
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
                delay=delay,
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
        if "subject" in data:
            existing_subject = collection.find_one({"subject": data["subject"]})
            if existing_subject:
                collection.delete_one({"subject": data["subject"]})
                data["uploadId"] = existing_subject.get("uploadId", 0)
        if "date" in data:
            data["date"] = datetime.now()

        if (
            "schedule" in data
            and data["schedule"] != ""
            and data["schedule"] != "Executed"
        ):
            data["schedule"] = datetime.fromisoformat(data["schedule"])
            timeSchedule(hours=data["schedule"].hour, minutes=data["schedule"].minute)
            print("Timing has been set to ", data["schedule"])

        for stage in ["stage1", "stage2", "stage3"]:
            if stage in data and data[stage] != False:
                data[stage] = datetime.now() + timedelta(days=int(data[stage]))
                timeSchedule(
                    data[stage].year,
                    data[stage].month,
                    data[stage].day,
                    data[stage].hour,
                    data[stage].minute,
                )
                print("Timing has been set to ", data["schedule"])

        result = collection.insert_one(data)
        return jsonify({"status": "success", "inserted_id": str(result.inserted_id)})
    except Exception as e:
        print(e)
        return jsonify({"status": "failed", "error": str(e)}), 500


@app.route("/latest_id", methods=["GET"])
def get_latest_id():
    subject = request.args.get("subject")
    if subject:
        latest_document = collection.find_one(
            {"subject": subject}, sort=[("uploadId", -1)]
        )
        if not latest_document:
            latest_document = collection.find_one(sort=[("uploadId", -1)])
    else:
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


@app.route("/isUserSigned", methods=["GET"])
def isUserSigned():
    user = request.args.get("user")
    if user:
        if user:
            token_file = f"{user.replace('@', '').replace('.com', '')}.json"
            return (
                jsonify(
                    {"status": "success", "isSignedIn": os.path.exists(token_file)}
                ),
                200,
            )
        else:
            return "Invalid parameters", 400
    else:
        return "Invalid parameters", 400


@app.route("/unsubscribe", methods=["GET"])
def unsubscribe():
    userID = request.args.get("userID")
    Email = request.args.get("Email")

    if not userID or not Email:
        return (
            jsonify(
                {"error": "Missing parameters. 'userID' and 'Email' are required."}
            ),
            400,
        )

    try:
        userID = int(userID)
    except ValueError:
        return jsonify({"error": "'userID' must be a valid integer."}), 400

    document = collection.find_one({"uploadId": userID})

    if not document:
        return jsonify({"error": "No document found for the given 'userID'."}), 404

    unsubscribe = document.get("unsubscribe", [])

    if Email not in unsubscribe:
        collection.update_one({"uploadId": userID}, {"$push": {"unsubscribe": Email}})
        return jsonify({"message": "Email successfully unsubscribed."}), 200
    else:
        return jsonify({"message": "Email is already unsubscribed."}), 200


if __name__ == "__main__":
    app.run(debug=True)
