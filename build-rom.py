import os
import sys
import time
import subprocess
import re
import glob
import argparse
import signal
import utils

# Config
BOT_TOKEN = os.environ.get("CONFIG_BOT_TOKEN")
CHAT_ID = os.environ.get("CONFIG_CHATID")
ERROR_CHAT_ID = os.environ.get("CONFIG_ERROR_CHATID", CHAT_ID)
DEVICE = os.environ.get("CONFIG_DEVICE")
TARGET = os.environ.get("CONFIG_BUILD_TARGET")
BUILD_VARIANT = os.environ.get("CONFIG_BUILD_TYPE")
USE_GOFILE = os.environ.get("CONFIG_GOFILE") == "true"

if not all([BOT_TOKEN, CHAT_ID, DEVICE, TARGET, BUILD_VARIANT]):
    print("ERROR: Missing configuration (BOT_TOKEN, CHATID, DEVICE, TARGET, or TYPE).")
    sys.exit(1)

cpu_cores = os.cpu_count()
jobs_env = os.environ.get("CONFIG_JOBS")
JOBS_FLAG = f"-j{jobs_env}" if jobs_env else (f"-j{cpu_cores}" if cpu_cores else "")
SYNC_JOBS = jobs_env if jobs_env else (str(cpu_cores) if cpu_cores else "4")

current_folder = os.getcwd().split("/")[-1]
ROM_NAME = os.environ.get("CONFIG_ROM_NAME") or current_folder or "Unknown ROM"

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


def main():
    global BUILD_PROCESS

    parser = argparse.ArgumentParser()
    parser.add_argument("-s", "--sync", action="store_true")
    parser.add_argument("-c", "--clean", action="store_true")
    args = parser.parse_args()

    # Sync
    if args.sync:
        start = time.time()
        details = f"{utils.line('Rom', ROM_NAME)}\n{utils.line('Jobs', SYNC_JOBS)}"
        msg_id = utils.send_msg(utils.MESSAGES["sync_start"].format(details=details))

        cmd = f"repo sync -c -j{SYNC_JOBS} --optimized-fetch --prune --force-sync --no-clone-bundle --no-tags"
        if subprocess.call(cmd.split()) != 0:
            subprocess.call(f"repo sync -j{SYNC_JOBS}".split())

        dur = utils.fmt_time(time.time() - start)
        utils.edit_msg(
            msg_id,
            utils.MESSAGES["sync_done"].format(
                details=utils.line("Rom", ROM_NAME), dur=dur
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
        f"<b>Rom:</b> <code>{ROM_NAME}</code>\n"
        f"<b>Device:</b> <code>{DEVICE}</code>\n"
        f"<b>Android:</b> <code>{ANDROID_VERSION}</code>\n"
        f"<b>Build ID:</b> <code>{BUILD_ID}</code>\n"
        f"<b>Type:</b> <code>{REAL_VARIANT}</code>"
    )

    msg_id = utils.send_msg(utils.MESSAGES["build_start"].format(base_info=base_info))

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
    detected_zip = None

    try:
        for log_line in BUILD_PROCESS.stdout:
            sys.stdout.write(log_line)
            log_file.write(log_line)

            if "Package Complete:" in log_line:
                detected_zip = log_line.split("Package Complete:")[1].strip().split()[0]

            if "Starting ninja..." in log_line:
                ninja_started = True

            match = regex.search(log_line)
            if match and ninja_started:
                pct, cnt, time_left = match.groups()
                now = time.time()

                if now - last_update > 15:
                    elapsed_str = utils.fmt_time(now - start_time)

                    # Build Stats
                    stats_str = f"<b>Progress:</b> <code>{pct} ({cnt})</code>\n"
                    if time_left:
                        clean_time = time_left.replace(" remaining", "").strip()
                        stats_str += f"<b>Remaining:</b> <code>{clean_time}</code>\n"
                    stats_str += f"<b>Elapsed:</b> <code>{elapsed_str}</code>"

                    utils.edit_msg(
                        msg_id,
                        utils.MESSAGES["build_progress"].format(
                            stats=stats_str, base_info=base_info
                        ),
                    )
                    last_update = now

        return_code = BUILD_PROCESS.wait()

    except Exception as e:
        print(f"Build Loop Error: {e}")
        return_code = 1
    finally:
        log_file.close()

    total_duration = utils.fmt_time(time.time() - start_time)

    # Failure
    if return_code != 0:
        utils.edit_msg(
            msg_id,
            utils.MESSAGES["build_fail"].format(
                time=total_duration, base_info=base_info
            ),
        )
        err_log = "out/error.log" if os.path.exists("out/error.log") else "build.log"
        utils.send_doc(err_log, ERROR_CHAT_ID)
        sys.exit(1)

    # Success
    final_build_msg = utils.MESSAGES["build_success"].format(
        time=total_duration, base_info=base_info
    )
    utils.edit_msg(
        msg_id, utils.MESSAGES["uploading"].format(build_msg=final_build_msg)
    )

    # Upload
    out_dir = f"out/target/product/{DEVICE}"
    final_zip = None
    if detected_zip and os.path.exists(detected_zip):
        final_zip = detected_zip
    else:
        zips = glob.glob(f"{out_dir}/*{DEVICE}*.zip")
        if zips:
            final_zip = max(zips, key=os.path.getctime)

    if not final_zip:
        utils.edit_msg(
            msg_id,
            utils.MESSAGES["upload_fail"].format(
                build_msg=final_build_msg, reason="No ZIP found."
            ),
        )
        sys.exit(1)

    upload_start = time.time()
    pd_link = utils.upload_pd(final_zip)
    gf_link = utils.upload_gofile(final_zip) if USE_GOFILE else None
    upload_duration = utils.fmt_time(time.time() - upload_start)

    size_mb = os.path.getsize(final_zip) / (1024 * 1024)
    size_str = f"{size_mb:.2f} MB"
    try:
        md5 = subprocess.check_output(["md5sum", final_zip], text=True).split()[0]
    except:
        md5 = "N/A"

    buttons_list = []
    if pd_link:
        buttons_list.append({"text": "PixelDrain", "url": pd_link})
    if USE_GOFILE and gf_link:
        buttons_list.append({"text": "GoFile", "url": gf_link})

    json_f = glob.glob(f"{out_dir}/*{DEVICE}*.json")
    if json_f:
        json_link = utils.upload_pd(json_f[0])
        if json_link:
            buttons_list.append({"text": "JSON", "url": json_link})

    file_name = os.path.basename(final_zip)

    if pd_link or gf_link:
        utils.edit_msg(
            msg_id,
            utils.MESSAGES["final_msg"].format(
                build_msg=final_build_msg,
                up_time=upload_duration,
                filename=file_name,
                size=size_str,
                md5=md5,
            ),
            buttons=[buttons_list] if buttons_list else None,
        )
    else:
        utils.edit_msg(
            msg_id,
            utils.MESSAGES["upload_fail"].format(
                build_msg=final_build_msg, reason="Could not upload files."
            ),
        )


if __name__ == "__main__":
    main()
