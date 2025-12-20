import os
import sys
import time
import subprocess
import re
import glob
import argparse
import utils

# Load config variables
BOT_TOKEN = os.environ.get("CONFIG_BOT_TOKEN")
CHAT_ID = os.environ.get("CONFIG_CHATID")
ERROR_CHAT_ID = os.environ.get("CONFIG_ERROR_CHATID", CHAT_ID)
DEVICE = os.environ.get("CONFIG_DEVICE")
TARGET = os.environ.get("CONFIG_BUILD_TARGET")
BUILD_VARIANT = os.environ.get("CONFIG_BUILD_TYPE")
USE_GOFILE = os.environ.get("CONFIG_GOFILE") == "true"
REC_IMAGES = os.environ.get("CONFIG_RECOVERY_IMAGES")

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

    utils.register_signal_handler(lambda: BUILD_PROCESS)

    parser = argparse.ArgumentParser()
    parser.add_argument("-s", "--sync", action="store_true")
    parser.add_argument("-c", "--clean", action="store_true")
    args = parser.parse_args()

    # Sync sources if requested
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

    # Clean output
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

    # Start build process
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

    # Monitor build output
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

    # Build failure
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

    # Build success
    final_build_msg = utils.MESSAGES["build_success"].format(
        time=total_duration, base_info=base_info
    )
    utils.edit_msg(
        msg_id, utils.MESSAGES["uploading"].format(build_msg=final_build_msg)
    )

    # Locate/Prepare artifacts
    # Locate the rom package
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

    # Packaging recovery images if requested
    rec_zip_path = None
    if REC_IMAGES:
        print("Packaging recovery images...")
        rec_list = re.split(r"[;\s]+", REC_IMAGES)
        cmd_files = []
        for img in rec_list:
            if not img:
                continue
            f_path = os.path.join(out_dir, img)
            if os.path.exists(f_path):
                cmd_files.append(f_path)

        if cmd_files:
            rec_name = f"RECOVERY-{os.path.basename(final_zip)}"
            subprocess.call(["zip", "-j", rec_name] + cmd_files)
            if os.path.exists(rec_name):
                rec_zip_path = rec_name

    # Upload files
    upload_start = time.time()

    files_to_upload = [("Download", final_zip)]
    if rec_zip_path:
        files_to_upload.append(("Recovery", rec_zip_path))

    json_f = glob.glob(f"{out_dir}/*{DEVICE}*.json")
    if json_f:
        files_to_upload.append(("JSON", json_f[0]))

    buttons_list = []
    main_file_uploaded = False

    for label, file_path in files_to_upload:
        if not file_path or not os.path.exists(file_path):
            continue

        pd_link = utils.upload_pd(file_path)
        if pd_link:
            buttons_list.append({"text": f"{label} (PD)", "url": pd_link})
            if file_path == final_zip:
                main_file_uploaded = True

        if USE_GOFILE:
            gf_link = utils.upload_gofile(file_path)
            if gf_link:
                buttons_list.append({"text": f"{label} (GF)", "url": gf_link})
                if file_path == final_zip:
                    main_file_uploaded = True

    upload_duration = utils.fmt_time(time.time() - upload_start)

    size_mb = os.path.getsize(final_zip) / (1024 * 1024)
    size_str = f"{size_mb:.2f} MB"
    try:
        md5 = subprocess.check_output(["md5sum", final_zip], text=True).split()[0]
    except:
        md5 = "N/A"

    file_name = os.path.basename(final_zip)

    if main_file_uploaded:
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
