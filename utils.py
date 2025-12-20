import os
import sys
import time
import json
import html
import base64
import requests
import signal
from dotenv import load_dotenv

# Load configs from .env file
load_dotenv("config.env")
BOT_TOKEN = os.environ.get("CONFIG_BOT_TOKEN")
CHAT_ID = os.environ.get("CONFIG_CHATID")
PD_API = os.environ.get("CONFIG_PDUP_API")

# Message templates for Telegram notifications
MESSAGES = {
    "sync_start": "<b>ℹ️ | Starting Synchronization...</b>\n{details}",
    "sync_done": "<b>✅ | Synchronization Complete!</b>\n{details}\n<b>Time:</b> {dur}",
    "build_start": "<b>ℹ️ | Starting Build...</b>\n\n{base_info}",
    "build_progress": ("<b>🔄 | Building...</b>\n" "{stats}\n\n" "{base_info}"),
    "build_fail": "<b>⚠️ | Build Failed</b>\n\nFailed after {time}\n\n{base_info}",
    "build_success": (
        "<b>✅ | Build Complete!</b>\n"
        "<b>Build Time:</b> <code>{time}</code>\n\n"
        "{base_info}"
    ),
    "uploading": "{build_msg}\n\n<b>🔄 | Uploading Files...</b>",
    "upload_fail": "{build_msg}\n\n<b>⚠️ | Upload Failed</b>\n\n{reason}",
    "final_msg": (
        "{build_msg}\n\n"
        "<b>✅ | Upload Complete</b>\n"
        "<b>Upload Time:</b> <code>{up_time}</code>\n\n"
        "<b>File:</b> <code>{filename}</code>\n"
        "<b>Size:</b> <code>{size}</code>\n"
        "<b>MD5:</b> <code>{md5}</code>"
    ),
}


# Formatting
def fmt_time(seconds):
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def line(label, value):
    return f"<b>{label}:</b> <code>{html.escape(str(value))}</code>"


# Telegram API
def tg_req(method, data, files=None, retries=3):
    if not BOT_TOKEN:
        print("Error: BOT_TOKEN missing in utils.")
        return {}

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    for attempt in range(retries):
        try:
            r = requests.post(url, data=data, files=files, timeout=30)
            if r.status_code == 200:
                return r.json()
            print(f"[Telegram Error {r.status_code}] {r.text}")
        except Exception as e:
            print(f"[Telegram Retry {attempt+1}/{retries}] {e}")
            time.sleep(2)
    return {}


def _get_tg_payload(chat_id, text, buttons=None, msg_id=None):
    data = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "html",
        "disable_web_page_preview": "true",
    }
    if msg_id:
        data["message_id"] = msg_id
    if buttons:
        data["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    return data


def send_msg(text, chat_id=CHAT_ID, buttons=None):
    if not chat_id:
        return None
    data = _get_tg_payload(chat_id, text, buttons)
    return tg_req("sendMessage", data).get("result", {}).get("message_id")


def edit_msg(msg_id, text, chat_id=CHAT_ID, buttons=None):
    if not msg_id or not chat_id:
        return
    data = _get_tg_payload(chat_id, text, buttons, msg_id)
    tg_req("editMessageText", data)


def send_doc(file_path, chat_id=CHAT_ID):
    if not chat_id:
        return
    if os.path.exists(file_path):
        with open(file_path, "rb") as f:
            tg_req(
                "sendDocument",
                {"chat_id": chat_id, "parse_mode": "html"},
                files={"document": f},
            )


# Upload for PixelDrain
def upload_pd(path):
    print(f"Uploading to PixelDrain: {path}")
    if not PD_API:
        print("PixelDrain API key missing.")
        return None

    file_name = os.path.basename(path)
    url = f"https://pixeldrain.com/api/file/{file_name}"

    auth_str = f":{PD_API}"
    auth_bytes = auth_str.encode("ascii")
    base64_auth = base64.b64encode(auth_bytes).decode("ascii")

    headers = {"Authorization": f"Basic {base64_auth}"}

    try:
        with open(path, "rb") as f:
            r = requests.put(url, data=f, headers=headers, timeout=300)

        if r.status_code in [200, 201]:
            return f"https://pixeldrain.com/u/{r.json().get('id')}"

        print(f"[PixelDrain Error {r.status_code}] {r.text}")
        return None
    except Exception as e:
        print(f"PixelDrain Upload Error: {e}")
        return None


# Upload for GoFile
def upload_gofile(path):
    print(f"Uploading to GoFile: {path}")
    try:
        server_req = requests.get("https://api.gofile.io/servers")
        data = server_req.json()
        if data["status"] != "ok":
            return None

        server = data["data"]["servers"][0]["name"]
        with open(path, "rb") as f:
            r = requests.post(
                f"https://{server}.gofile.io/uploadFile",
                files={"file": f},
                timeout=300,
            )
        if r.status_code == 200:
            return r.json()["data"]["downloadPage"]
        return None
    except Exception as e:
        print(f"GoFile Upload Error: {e}")
        return None


# Signal handler to kill build processes
def register_signal_handler(process_getter):

    def handler(sig, frame):
        print("\n[BOT] Interruption detected. Exiting...")
        process = process_getter()

        if process and process.poll() is None:
            print("[BOT] Killing build process...")
            process.terminate()
            time.sleep(1)
            if process.poll() is None:
                process.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, handler)
