import os
import sys
import time
import subprocess
import re
import glob
import argparse
import requests
import html
import signal
import json

# Config
if os.path.exists("config.env"):
    with open("config.env", "r") as f:
        for line_content in f:
            if "=" in line_content and not line_content.strip().startswith("#"):
                k, v = line_content.strip().split("=", 1)
                os.environ[k] = v.strip('"').strip("'")

BOT_TOKEN = os.environ.get("CONFIG_BOT_TOKEN")
CHAT_ID = os.environ.get("CONFIG_CHATID")

if not BOT_TOKEN or not CHAT_ID:
    print("ERROR: CONFIG_BOT_TOKEN or CONFIG_CHATID missing.")
    sys.exit(1)

ERROR_CHAT_ID = os.environ.get("CONFIG_ERROR_CHATID", CHAT_ID)
DEVICE = os.environ.get("CONFIG_DEVICE")
TARGET = os.environ.get("CONFIG_BUILD_TARGET")
BUILD_VARIANT = os.environ.get("CONFIG_BUILD_TYPE")
PD_API = os.environ.get("CONFIG_PDUP_API")
USE_GOFILE = os.environ.get("CONFIG_GOFILE") == "true"

if not all([DEVICE, TARGET, BUILD_VARIANT]):
    print("ERROR: Missing build configuration (DEVICE, TARGET, or TYPE).")
    sys.exit(1)

cpu_cores = os.cpu_count()
jobs_env = os.environ.get("CONFIG_JOBS")
JOBS_FLAG = f"-j{jobs_env}" if jobs_env else (f"-j{cpu_cores}" if cpu_cores else "")
SYNC_JOBS = jobs_env if jobs_env else (str(cpu_cores) if cpu_cores else "4")

current_folder = os.getcwd().split("/")[-1]
ROM_NAME = os.environ.get("CONFIG_ROM_NAME") or current_folder or "Unknown ROM"

# Global process handle for graceful exit
BUILD_PROCESS = None


def signal_handler(sig, frame):
    global BUILD_PROCESS
    print("\n[BOT] Interruption detected. Exiting...")
    if BUILD_PROCESS and BUILD_PROCESS.poll() is None:
        print("[BOT] Killing build process...")
        BUILD_PROCESS.terminate()
        time.sleep(1)
        if BUILD_PROCESS.poll() is None:
            BUILD_PROCESS.kill()
    sys.exit(0)


signal.signal(signal.SIGINT, signal_handler)


# Helpers
def fmt_time(seconds):
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def get_build_vars():
    print("Fetching build system variables...")
    try:
        cmd = (
            f"source build/envsetup.sh && "
            f"breakfast {DEVICE} {BUILD_VARIANT} >/dev/null 2>&1 && "
            f'echo "VER=$(get_build_var PLATFORM_VERSION)" && '
            f'echo "BID=$(get_build_var BUILD_ID)" && '
            f'echo "TYPE=$(get_build_var TARGET_BUILD_VARIANT)"'
        )
        output = subprocess.check_output(
            cmd, shell=True, executable="/bin/bash", text=True
        )
        d = {}
        for out_line in output.splitlines():
            if "=" in out_line:
                k, v = out_line.split("=", 1)
                d[k] = v.strip()
        return d
    except Exception as e:
        print(f"Warning: Could not fetch vars: {e}")
        return {"VER": "N/A", "BID": "N/A", "TYPE": BUILD_VARIANT}


def tg_req(method, data, files=None, retries=3):
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


def send_msg(text, chat=CHAT_ID, buttons=None):
    data = {
        "chat_id": chat,
        "text": text,
        "parse_mode": "html",
        "disable_web_page_preview": "true",
    }
    if buttons:
        data["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    return tg_req("sendMessage", data).get("result", {}).get("message_id")


def edit_msg(msg_id, text, chat=CHAT_ID, buttons=None):
    if not msg_id:
        return
    data = {
        "chat_id": chat,
        "message_id": msg_id,
        "text": text,
        "parse_mode": "html",
        "disable_web_page_preview": "true",
    }
    if buttons:
        data["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    tg_req("editMessageText", data)


def send_doc(file_path, chat=CHAT_ID):
    if os.path.exists(file_path):
        with open(file_path, "rb") as f:
            tg_req(
                "sendDocument",
                {"chat_id": chat, "parse_mode": "html"},
                files={"document": f},
            )


def line(label, value):
    return f"<b>{label}:</b> <code>{html.escape(str(value))}</code>"


def format_msg(icon, title, details, footer=""):
    header = f"<b>{icon} | {title}</b>"
    msg = f"{header}\n{details}"
    if footer:
        msg += f"\n\n<i>{html.escape(footer)}</i>"
    return msg


def upload_pd(path):
    print(f"Uploading to PixelDrain: {path}")
    try:
        r = requests.put(
            "https://pixeldrain.com/api/file/",
            data=open(path, "rb"),
            auth=("", PD_API) if PD_API else None,
            timeout=300,
        )
        if r.status_code == 200:
            return f"https://pixeldrain.com/u/{r.json().get('id')}"
        return None
    except Exception as e:
        print(f"PixelDrain Upload Error: {e}")
        return None


def upload_gofile(path):
    print(f"Uploading to GoFile: {path}")
    try:
        server_req = requests.get("https://api.gofile.io/servers")
        server = server_req.json()["data"]["servers"][0]["name"]
        r = requests.post(
            f"https://{server}.gofile.io/uploadFile",
            files={"file": open(path, "rb")},
            timeout=300,
        )
        if r.status_code == 200:
            return r.json()["data"]["downloadPage"]
        return None
    except Exception as e:
        print(f"GoFile Upload Error: {e}")
        return None


# Main
def main():
    global BUILD_PROCESS

    parser = argparse.ArgumentParser()
    parser.add_argument("-s", "--sync", action="store_true")
    parser.add_argument("-c", "--clean", action="store_true")
    args = parser.parse_args()

    # Sync
    if args.sync:
        start = time.time()
        details = f"{line('rom', ROM_NAME)}\n{line('jobs', SYNC_JOBS)}"
        msg_id = send_msg(format_msg("ℹ️", "Starting...", details))

        cmd = f"repo sync -c -j{SYNC_JOBS} --optimized-fetch --prune --force-sync --no-clone-bundle --no-tags"
        if subprocess.call(cmd.split()) != 0:
            subprocess.call(f"repo sync -j{SYNC_JOBS}".split())

        dur = fmt_time(time.time() - start)
        edit_msg(
            msg_id,
            format_msg(
                "✅", "Sync Complete!", f"{line('rom', ROM_NAME)}", f"Took {dur}"
            ),
        )

    # Clean
    if args.clean and os.path.exists("out"):
        import shutil

        shutil.rmtree("out")

    # Build Setup
    build_vars = get_build_vars()
    ANDROID_VERSION = build_vars.get("VER", "N/A")
    BUILD_ID = build_vars.get("BID", "N/A")
    REAL_VARIANT = build_vars.get("TYPE", BUILD_VARIANT)

    base_info = (
        f"<b>rom:</b> <code>{ROM_NAME}</code>\n"
        f"<b>device:</b> <code>{DEVICE}</code>\n"
        f"<b>android:</b> <code>{ANDROID_VERSION}</code>\n"
        f"<b>build id:</b> <code>{BUILD_ID}</code>\n"
        f"<b>build type:</b> <code>{REAL_VARIANT}</code>"
    )

    # Starting message: Status -> Info
    msg_id = send_msg(f"<b>ℹ️ | Starting...</b>\n\n{base_info}")

    build_cmd = f"source build/envsetup.sh && breakfast {DEVICE} {BUILD_VARIANT} && m {TARGET} {JOBS_FLAG}"
    print(f"Cmd: {build_cmd}")

    log_file = open("build.log", "w")
    start_time = time.time()

    BUILD_PROCESS = subprocess.Popen(
        build_cmd,
        shell=True,
        executable="/bin/bash",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    regex = re.compile(r"\[\s*(\d+%)\s+(\d+/\d+)(?: (.*?remaining))?.*\]")
    last_update = 0
    ninja_started = False

    try:
        for log_line in BUILD_PROCESS.stdout:
            sys.stdout.write(log_line)
            log_file.write(log_line)

            if "Starting ninja..." in log_line:
                ninja_started = True

            match = regex.search(log_line)
            if match and ninja_started:
                pct, cnt, time_left = match.groups()
                now = time.time()

                if now - last_update > 15:
                    elapsed_str = fmt_time(now - start_time)

                    prog_text = f"{pct} ({cnt})"
                    remaining_line = ""
                    if time_left:
                        clean_time = time_left.replace(" remaining", "").strip()
                        remaining_line = (
                            f"<b>remaining:</b> <code>{clean_time}</code>\n"
                        )

                    edit_msg(
                        msg_id,
                        f"<b>🔄 | Building...</b>\n"
                        f"<b>progress:</b> <code>{prog_text}</code>\n"
                        f"{remaining_line}"
                        f"<b>elapsed:</b> <code>{elapsed_str}</code>\n\n"
                        f"{base_info}",
                    )
                    last_update = now

        return_code = BUILD_PROCESS.wait()

    except Exception as e:
        print(f"Build Loop Error: {e}")
        return_code = 1
    finally:
        log_file.close()

    total_duration = fmt_time(time.time() - start_time)

    # Failure Handling
    if return_code != 0:
        edit_msg(
            msg_id,
            f"<b>⚠️ | Build fail</b>\n\nFailed after {total_duration}\n\n{base_info}",
        )
        err_log = "out/error.log" if os.path.exists("out/error.log") else "build.log"
        send_doc(err_log, ERROR_CHAT_ID)
        sys.exit(1)

    # Build Success
    final_build_msg = (
        f"<b>✅ | Build Complete!</b>\n"
        f"<b>Build time:</b> <code>{total_duration}</code>\n\n"
        f"{base_info}"
    )

    edit_msg(msg_id, f"{final_build_msg}\n\n<b>🔄 | Uploading...</b>")

    # Upload Start
    out_dir = f"out/target/product/{DEVICE}"
    zips = glob.glob(f"{out_dir}/*{DEVICE}*.zip")

    if not zips:
        edit_msg(
            msg_id,
            f"{final_build_msg}\n\n<b>⚠️ | Upload fail</b>\n\nNo ZIP found after build.",
        )
        sys.exit(1)

    final_zip = max(zips, key=os.path.getctime)

    upload_start = time.time()
    pd_link = upload_pd(final_zip)
    gf_link = upload_gofile(final_zip) if USE_GOFILE else None
    upload_duration = fmt_time(time.time() - upload_start)

    # Upload Stats
    size_mb = os.path.getsize(final_zip) / (1024 * 1024)
    size_str = f"{size_mb:.2f} MB"
    try:
        md5 = subprocess.check_output(["md5sum", final_zip], text=True).split()[0]
    except:
        md5 = "N/A"

    # Buttons
    buttons_list = []
    if pd_link:
        buttons_list.append({"text": "PixelDrain", "url": pd_link})
    if USE_GOFILE and gf_link:
        buttons_list.append({"text": "GoFile", "url": gf_link})

    # Optional JSON
    json_f = glob.glob(f"{out_dir}/*{DEVICE}*.json")
    if json_f:
        json_link = upload_pd(json_f[0])
        if json_link:
            buttons_list.append({"text": "JSON", "url": json_link})

    file_name = os.path.basename(final_zip)

    # Final Message
    final_combined_msg = (
        f"{final_build_msg}\n\n"
        f"<b>✅ | Upload completo</b>\n"
        f"<b>Upload time:</b> <code>{upload_duration}</code>\n\n"
        f"<b>file:</b> <code>{file_name}</code>\n"
        f"<b>size:</b> <code>{size_str}</code>\n"
        f"<b>md5:</b> <code>{md5}</code>"
    )

    if pd_link or gf_link:
        edit_msg(
            msg_id, final_combined_msg, buttons=[buttons_list] if buttons_list else None
        )
    else:
        edit_msg(
            msg_id,
            f"{final_build_msg}\n\n<b>⚠️ | Upload fail</b>\n\nCould not upload files.",
        )


if __name__ == "__main__":
    main()
