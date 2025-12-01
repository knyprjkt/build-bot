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
from datetime import timedelta

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
ROM_NAME = current_folder if current_folder else "Unknown ROM"

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


def send_msg(text, chat=CHAT_ID):
    return (
        tg_req(
            "sendMessage",
            {
                "chat_id": chat,
                "text": text,
                "parse_mode": "html",
                "disable_web_page_preview": "true",
            },
        )
        .get("result", {})
        .get("message_id")
    )


def edit_msg(msg_id, text, chat=CHAT_ID):
    if not msg_id:
        return
    tg_req(
        "editMessageText",
        {
            "chat_id": chat,
            "message_id": msg_id,
            "text": text,
            "parse_mode": "html",
            "disable_web_page_preview": "true",
        },
    )


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
    msg = f"{header}\n\n{details}"
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
        msg_id = send_msg(format_msg("🟡", "Syncing sources...", details))

        cmd = f"repo sync -c -j{SYNC_JOBS} --optimized-fetch --prune --force-sync --no-clone-bundle --no-tags"
        if subprocess.call(cmd.split()) != 0:
            subprocess.call(f"repo sync -j{SYNC_JOBS}".split())

        dur = str(timedelta(seconds=int(time.time() - start)))
        edit_msg(
            msg_id,
            format_msg(
                "🟢", "Sources synced!", f"{line('rom', ROM_NAME)}", f"Took {dur}"
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
        f"{line('rom', ROM_NAME)}\n"
        f"{line('device', DEVICE)}\n"
        f"{line('android', ANDROID_VERSION)}\n"
        f"{line('build id', BUILD_ID)}\n"
        f"{line('build type', REAL_VARIANT)}"
    )

    initial_txt = (
        f"{base_info}\n{line('progress', 'Initializing...')}\n{line('elapsed', '0s')}"
    )
    msg_id = send_msg(format_msg("🟡", "Compiling ROM...", initial_txt))

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

                # Update Telegram every 15 seconds to avoid rate limits
                if now - last_update > 15:
                    elapsed_str = str(timedelta(seconds=int(now - start_time)))
                    progress_val = f"{pct} ({cnt})"
                    if time_left:
                        clean_time = time_left.replace(" remaining", "").strip()
                        progress_val += f" remaining: {clean_time}"

                    new_details = f"{base_info}\n{line('progress', progress_val)}\n{line('elapsed', elapsed_str)}"
                    edit_msg(msg_id, format_msg("🟡", "Compiling ROM...", new_details))
                    last_update = now

        return_code = BUILD_PROCESS.wait()

    except Exception as e:
        print(f"Build Loop Error: {e}")
        return_code = 1
    finally:
        log_file.close()

    total_duration = str(timedelta(seconds=int(time.time() - start_time)))

    # Failure Handling
    if return_code != 0:
        edit_msg(
            msg_id,
            format_msg("🔴", "Build Failed", "", f"Failed after {total_duration}"),
        )
        err_log = "out/error.log" if os.path.exists("out/error.log") else "build.log"
        send_doc(err_log, ERROR_CHAT_ID)
        sys.exit(1)

    # Success & Upload
    out_dir = f"out/target/product/{DEVICE}"
    zips = glob.glob(f"{out_dir}/*{DEVICE}*.zip")

    if not zips:
        edit_msg(
            msg_id,
            format_msg(
                "🔴", "No ZIP found!", "", "Build finished but no file generated."
            ),
        )
        sys.exit(1)

    final_zip = max(zips, key=os.path.getctime)
    edit_msg(msg_id, format_msg("🟡", "Uploading...", "Uploading artifacts..."))

    pd_link = upload_pd(final_zip)
    gf_link = upload_gofile(final_zip) if USE_GOFILE else None

    # Final Stats
    size_mb = os.path.getsize(final_zip) / (1024 * 1024)
    size_str = f"{size_mb:.2f} MB"
    try:
        md5 = subprocess.check_output(["md5sum", final_zip], text=True).split()[0]
    except:
        md5 = "N/A"

    final_details = f"{base_info}\n{line('size', size_str)}\n{line('md5', md5)}"

    links_html = ""
    if pd_link:
        links_html += f"\n<b>PixelDrain:</b> <a href='{pd_link}'>Download</a>"
    else:
        links_html += "\n<b>PixelDrain:</b> Upload Failed"

    if USE_GOFILE:
        if gf_link:
            links_html += f"\n<b>GoFile:</b> <a href='{gf_link}'>Download</a>"
        else:
            links_html += "\n<b>GoFile:</b> Upload Failed"

    # Optional JSON
    json_f = glob.glob(f"{out_dir}/*{DEVICE}*.json")
    if json_f:
        json_link = upload_pd(json_f[0])
        if json_link:
            links_html += f"\n<b>JSON:</b> <a href='{json_link}'>Download</a>"

    edit_msg(
        msg_id,
        format_msg(
            "🟢",
            "Build Complete!",
            final_details + "\n" + links_html,
            f"Total time: {total_duration}",
        ),
    )


if __name__ == "__main__":
    main()
