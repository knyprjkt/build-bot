import os
import sys
import time
import subprocess
import shutil
import argparse
import signal
import re
from datetime import datetime
import utils

# Configs
BOT_TOKEN = os.environ.get("CONFIG_BOT_TOKEN")
CHAT_ID = os.environ.get("CONFIG_CHATID")
ERROR_CHAT_ID = os.environ.get("CONFIG_ERROR_CHATID", CHAT_ID)
DEFCONFIG = os.environ.get("CONFIG_DEFCONFIG")
AK3_REPO = os.environ.get("CONFIG_AK3_REPO")
USE_GOFILE = os.environ.get("CONFIG_GOFILE") == "true"

if not all([BOT_TOKEN, CHAT_ID, DEFCONFIG]):
    print("ERROR: Missing configuration (BOT_TOKEN, CHATID, or DEFCONFIG).")
    sys.exit(1)

cpu_cores = os.cpu_count()
jobs_env = os.environ.get("CONFIG_JOBS")
JOBS_FLAG = f"-j{jobs_env}" if jobs_env else (f"-j{cpu_cores}" if cpu_cores else "-j4")
DISPLAY_JOBS = jobs_env if jobs_env else (f"{cpu_cores} (All)" if cpu_cores else "4")

KERNEL_OUT = "out/arch/arm64/boot"
ANYKERNEL_DIR = "AnyKernel3"
LOG_FILE = "build.log"

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


def get_git_head():
    try:
        short_hash = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True
        ).strip()
        full_hash = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip()
        origin = subprocess.check_output(
            ["git", "remote", "get-url", "origin"], text=True
        ).strip()

        if origin.endswith(".git"):
            origin = origin[:-4]

        return f"<a href='{origin}/commit/{full_hash}'>{short_hash}</a>"
    except Exception:
        return "Unknown"


def get_localversion():
    config_path = "out/.config"
    if not os.path.exists(config_path):
        return "N/A"
    try:
        with open(config_path, "r") as f:
            for file_line in f:
                if file_line.strip().startswith("CONFIG_LOCALVERSION="):
                    return file_line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return "N/A"


def get_compiler_version():
    try:
        cmd = "clang --version | head -n 1"
        output = subprocess.check_output(cmd, shell=True, text=True).strip()
        match = re.search(r"clang version \d+\.\d+\.\d+", output)
        if match:
            return match.group(0)
        if "clang version" in output:
            return "Clang " + output.split("clang version")[-1].strip().split()[0]
    except Exception:
        pass
    return "Clang/LLVM"


def get_compiled_version_string():
    image_path = os.path.join(KERNEL_OUT, "Image")
    if not os.path.exists(image_path):
        return None
    try:
        cmd = f"strings {image_path} | grep 'Linux version [0-9]' | head -n 1"
        line_out = subprocess.check_output(cmd, shell=True, text=True).strip()
        match = re.search(r"Linux version (\S+)", line_out)
        if match:
            return match.group(1)
    except Exception as e:
        print(f"Version Extraction Error: {e}")
        pass
    return None


def package_anykernel(version_string):
    print("Packaging AnyKernel3...")
    if os.path.exists(ANYKERNEL_DIR):
        shutil.rmtree(ANYKERNEL_DIR)

    subprocess.call(["git", "clone", "-q", AK3_REPO, ANYKERNEL_DIR])

    files_map = {"Image.gz": "Image.gz", "dtbo.img": "dtbo.img", "dtb.img": "dtb"}

    for src_name, dst_name in files_map.items():
        src = os.path.join(KERNEL_OUT, src_name)
        dst = os.path.join(ANYKERNEL_DIR, dst_name)
        if os.path.exists(src):
            shutil.copy(src, dst)

    timestamp = datetime.now().strftime("%Y%a%b%d-%H%M%S")
    ver_tag = version_string if version_string else "Unknown-Kernel"
    zip_name = f"{ver_tag}-{timestamp}.zip"

    cwd = os.getcwd()
    os.chdir(ANYKERNEL_DIR)
    zip_cmd = [
        "zip",
        "-r9",
        "-q",
        f"../{zip_name}",
        ".",
        "-x",
        ".git*",
        "README.md",
        "*placeholder",
        ".gitignore",
    ]
    subprocess.call(zip_cmd)
    os.chdir(cwd)

    if os.path.exists(zip_name):
        return os.path.abspath(zip_name)
    return None


def main():
    global BUILD_PROCESS
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--clean", action="store_true")
    args = parser.parse_args()

    if args.clean and os.path.exists("out"):
        print("Cleaning out/...")
        shutil.rmtree("out")

    git_head_link = get_git_head()
    compiler_ver = get_compiler_version()

    base_info = (
        f"<b>Head:</b> <code>{git_head_link}</code>\n"
        f"{utils.line('Defconfig', DEFCONFIG)}\n"
        f"{utils.line('Jobs', DISPLAY_JOBS)}\n"
        f"{utils.line('Compiler', compiler_ver)}"
    )

    msg_id = utils.send_msg(utils.MESSAGES["build_start"].format(base_info=base_info))

    print(f"Configuring: {DEFCONFIG}")
    subprocess.call(
        f"make O=out ARCH=arm64 LLVM=1 {DEFCONFIG}", shell=True, executable="/bin/bash"
    )

    local_ver = get_localversion()

    # Update info with local version
    base_info = (
        f"<b>Head:</b> <code>{git_head_link}</code>\n"
        f"{utils.line('Local Version', local_ver)}\n"
        f"{utils.line('Defconfig', DEFCONFIG)}\n"
        f"{utils.line('Jobs', DISPLAY_JOBS)}\n"
        f"{utils.line('Compiler', compiler_ver)}"
    )

    build_cmd = f"make {JOBS_FLAG} O=out ARCH=arm64 LLVM=1 Image.gz dtbo.img dtb.img"
    print(f"Building: {build_cmd}")

    start_time = time.time()
    log_file = open(LOG_FILE, "w")

    BUILD_PROCESS = subprocess.Popen(
        build_cmd,
        shell=True,
        executable="/bin/bash",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    last_update = 0

    try:
        while True:
            line_out = BUILD_PROCESS.stdout.readline()
            if not line_out and BUILD_PROCESS.poll() is not None:
                break
            if not line_out:
                time.sleep(0.1)
                continue

            log_file.write(line_out)

            now = time.time()
            if now - last_update > 15:
                elapsed = utils.fmt_time(now - start_time)

                stats_str = f"<b>Elapsed:</b> <code>{elapsed}</code>"

                utils.edit_msg(
                    msg_id,
                    utils.MESSAGES["build_progress"].format(
                        stats=stats_str, base_info=base_info
                    ),
                )
                last_update = now

        return_code = BUILD_PROCESS.poll()
        if return_code is None:
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
        utils.send_doc(LOG_FILE, ERROR_CHAT_ID)
        sys.exit(1)

    # Success
    final_build_msg = utils.MESSAGES["build_success"].format(
        time=total_duration, base_info=base_info
    )
    utils.edit_msg(
        msg_id, utils.MESSAGES["uploading"].format(build_msg=final_build_msg)
    )

    # Package
    compiled_ver_str = get_compiled_version_string()
    final_zip = package_anykernel(compiled_ver_str)

    if not final_zip:
        utils.edit_msg(
            msg_id,
            utils.MESSAGES["upload_fail"].format(
                build_msg=final_build_msg, reason="Could not create ZIP."
            ),
        )
        sys.exit(1)

    # Upload
    file_name = os.path.basename(final_zip)
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
