import os
import sys
import json
import time
import threading
import subprocess
import urllib.parse

import requests
import websocket


# ============================================================
# CONFIGURATION
# ============================================================

EDGE_PATH = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

CDP_PORT = 9222

SCRIPT_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

EDGE_PROFILE = os.path.join(
    SCRIPT_DIR,
    "edge-range-capture-profile"
)

DRIVE_URL = (
    "https://drive.google.com/file/"
    "d/1yv-WNLJvBGkepqC55rZqHXZsPVQPwaRj/view"
)

OUTPUT_JSON = os.path.join(
    SCRIPT_DIR,
    "drive_playback_requests.json"
)

CDP_TIMEOUT = 5

# ------------------------------------------------------------
# Capture duration
#
# After you press PLAY, the script captures for this many
# seconds and then exits automatically.
# ------------------------------------------------------------

CAPTURE_SECONDS = 60


# ============================================================
# STATE
# ============================================================

captured = []

captured_lock = threading.Lock()

stop_capture = threading.Event()

sequence = 0


# ============================================================
# LOGGING
# ============================================================

def log(message=""):
    print(message, flush=True)


def die(message):

    log()
    log("============================================================")
    log("ERROR")
    log("============================================================")
    log(message)
    log()

    sys.exit(1)


# ============================================================
# HELPERS
# ============================================================

def get_query(url):

    parsed = urllib.parse.urlsplit(url)

    return dict(
        urllib.parse.parse_qsl(
            parsed.query,
            keep_blank_values=True
        )
    )


def get_itag(url):

    return get_query(url).get("itag")


def get_range_from_url(url):

    return get_query(url).get("range")


def is_media_url(url):

    if "videoplayback" not in url:
        return False

    return (
        "c.drive.google.com" in url
        or "googlevideo.com" in url
    )


def header_value(headers, name):

    name = name.lower()

    for key, value in headers.items():

        if key.lower() == name:
            return value

    return None


def parse_content_range(value):

    if not value:
        return None

    try:

        if not value.startswith("bytes "):
            return None

        value = value[6:]

        range_part, total = value.split("/", 1)

        start, end = range_part.split("-", 1)

        return {
            "start": int(start),
            "end": int(end),
            "total": (
                None
                if total == "*"
                else int(total)
            )
        }

    except Exception:

        return None


def parse_range(value):

    if not value:
        return None

    try:

        start, end = value.split("-", 1)

        return {
            "start": int(start),
            "end": int(end),
            "length": int(end) - int(start) + 1
        }

    except Exception:

        return None


# ============================================================
# SAVE REQUEST
# ============================================================

def save_request(
    target_name,
    request_id,
    url,
    request_headers=None,
    response=None
):

    global sequence

    itag = get_itag(url)

    if itag not in (
        "136",
        "140"
    ):
        return

    query = get_query(url)

    with captured_lock:

        sequence += 1

        entry = {
            "sequence": sequence,

            "timestamp": time.time(),

            "target": target_name,

            "request_id": request_id,

            "itag": itag,

            "url": url,

            "host": urllib.parse.urlsplit(url).netloc,

            "url_query": query,

            "url_range": query.get("range"),

            "url_range_parsed":
                parse_range(
                    query.get("range")
                ),

            "request_number":
                query.get("rn"),

            "request_buffer":
                query.get("rbuf"),

            "ump":
                query.get("ump"),

            "request_headers":
                request_headers or {}
        }

        if response:

            headers = response.get(
                "headers",
                {}
            )

            content_range = header_value(
                headers,
                "content-range"
            )

            content_length = header_value(
                headers,
                "content-length"
            )

            content_type = header_value(
                headers,
                "content-type"
            )

            entry["response"] = {
                "url": response.get("url"),

                "status":
                    response.get("status"),

                "status_text":
                    response.get("statusText"),

                "mime_type":
                    response.get("mimeType"),

                "headers":
                    headers,

                "content_type":
                    content_type,

                "content_range":
                    content_range,

                "content_range_parsed":
                    parse_content_range(
                        content_range
                    ),

                "content_length":
                    content_length
            }

            entry["encoded_data_length"] = response.get(
                "encodedDataLength"
            )

            entry["from_disk_cache"] = response.get(
                "fromDiskCache"
            )

            entry["from_service_worker"] = response.get(
                "fromServiceWorker"
            )

        captured.append(entry)

        current_sequence = sequence

    # --------------------------------------------------------
    # Console
    # --------------------------------------------------------

    log()
    log("------------------------------------------------------------")
    log(
        f"[{target_name}] "
        f"MEDIA EVENT #{current_sequence}"
    )
    log("------------------------------------------------------------")

    log(
        f"itag: {itag}"
    )

    log(
        f"Host: "
        f"{urllib.parse.urlsplit(url).netloc}"
    )

    log(
        f"URL range: "
        f"{query.get('range', 'NONE')}"
    )

    log(
        f"rn: "
        f"{query.get('rn', 'NONE')}"
    )

    log(
        f"rbuf: "
        f"{query.get('rbuf', 'NONE')}"
    )

    log(
        f"ump: "
        f"{query.get('ump', 'NONE')}"
    )

    if response:

        log(
            f"HTTP status: "
            f"{response.get('status')}"
        )

        log(
            f"MIME: "
            f"{response.get('mimeType', '')}"
        )

        headers = response.get(
            "headers",
            {}
        )

        content_range = header_value(
            headers,
            "content-range"
        )

        content_length = header_value(
            headers,
            "content-length"
        )

        log(
            f"Content-Range: "
            f"{content_range or 'NONE'}"
        )

        log(
            f"Content-Length: "
            f"{content_length or 'NONE'}"
        )

    log()


# ============================================================
# UPDATE LOADING FINISHED DATA
# ============================================================

def update_loading_finished(
    request_id,
    encoded_data_length
):

    with captured_lock:

        for item in reversed(captured):

            if (
                item.get("request_id")
                == request_id
            ):

                item[
                    "loading_finished_encoded_data_length"
                ] = encoded_data_length

                return


# ============================================================
# CDP LISTENER
# ============================================================

def network_listener(
    target_name,
    target
):

    ws_url = target.get(
        "webSocketDebuggerUrl"
    )

    if not ws_url:

        log(
            f"[{target_name}] "
            "No websocket debugger URL."
        )

        return

    log(
        f"[{target_name}] Connecting..."
    )

    try:

        ws = websocket.create_connection(
            ws_url,
            timeout=CDP_TIMEOUT,
            suppress_origin=True
        )

    except Exception as e:

        log(
            f"[{target_name}] "
            f"Connection failed: {e}"
        )

        return

    requests_seen = {}

    try:

        # ----------------------------------------------------
        # Enable Network
        # ----------------------------------------------------

        ws.send(
            json.dumps(
                {
                    "id": 1,
                    "method": "Network.enable",
                    "params": {
                        "maxTotalBufferSize":
                            100 * 1024 * 1024,

                        "maxResourceBufferSize":
                            50 * 1024 * 1024
                    }
                }
            )
        )

        log(
            f"[{target_name}] "
            "Network monitoring enabled."
        )

        # ----------------------------------------------------
        # Main loop
        # ----------------------------------------------------

        while not stop_capture.is_set():

            try:

                raw = ws.recv()

            except websocket.WebSocketTimeoutException:

                continue

            except websocket.WebSocketConnectionClosedException:

                log(
                    f"[{target_name}] "
                    "WebSocket closed."
                )

                break

            except Exception as e:

                log(
                    f"[{target_name}] "
                    f"Receive error: {e}"
                )

                break

            if not raw:
                continue

            try:

                message = json.loads(raw)

            except Exception:

                continue

            method = message.get(
                "method"
            )

            params = message.get(
                "params",
                {}
            )

            # =================================================
            # REQUEST
            # =================================================

            if method == "Network.requestWillBeSent":

                request_id = params.get(
                    "requestId"
                )

                request = params.get(
                    "request",
                    {}
                )

                url = request.get(
                    "url",
                    ""
                )

                if not is_media_url(url):
                    continue

                itag = get_itag(url)

                if itag not in (
                    "136",
                    "140"
                ):
                    continue

                requests_seen[
                    request_id
                ] = {
                    "url": url,

                    "request": request,

                    "timestamp":
                        params.get(
                            "timestamp"
                        ),

                    "wall_time":
                        params.get(
                            "wallTime"
                        )
                }

                save_request(
                    target_name,
                    request_id,
                    url,
                    request.get(
                        "headers",
                        {}
                    )
                )

            # =================================================
            # RESPONSE
            # =================================================

            elif method == "Network.responseReceived":

                request_id = params.get(
                    "requestId"
                )

                response = params.get(
                    "response",
                    {}
                )

                url = response.get(
                    "url",
                    ""
                )

                if not is_media_url(url):
                    continue

                itag = get_itag(url)

                if itag not in (
                    "136",
                    "140"
                ):
                    continue

                request_info = requests_seen.get(
                    request_id
                )

                request_headers = {}

                if request_info:

                    request_headers = (
                        request_info
                        .get("request", {})
                        .get("headers", {})
                    )

                save_request(
                    target_name,
                    request_id,
                    url,
                    request_headers,
                    response
                )

            # =================================================
            # LOADING FINISHED
            # =================================================

            elif method == "Network.loadingFinished":

                request_id = params.get(
                    "requestId"
                )

                if request_id not in requests_seen:
                    continue

                encoded_data_length = params.get(
                    "encodedDataLength"
                )

                update_loading_finished(
                    request_id,
                    encoded_data_length
                )

            # =================================================
            # REQUEST EXTRA INFO
            # =================================================

            elif method == "Network.requestWillBeSentExtraInfo":

                request_id = params.get(
                    "requestId"
                )

                if request_id not in requests_seen:
                    continue

                request_info = requests_seen[
                    request_id
                ]

                request_info[
                    "extra_headers"
                ] = params.get(
                    "headers",
                    {}
                )

    finally:

        try:
            ws.close()

        except Exception:
            pass

        log(
            f"[{target_name}] "
            "Listener stopped."
        )


# ============================================================
# EDGE
# ============================================================

def launch_edge():

    os.makedirs(
        EDGE_PROFILE,
        exist_ok=True
    )

    log()
    log("Launching Edge...")
    log()
    log("Drive URL:")
    log(DRIVE_URL)
    log()

    command = [
        EDGE_PATH,

        f"--remote-debugging-port={CDP_PORT}",

        "--remote-allow-origins="
        f"http://127.0.0.1:{CDP_PORT}",

        f"--user-data-dir={EDGE_PROFILE}",

        "--no-first-run",

        "--no-default-browser-check",

        DRIVE_URL
    ]

    subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )


def wait_for_cdp():

    url = (
        f"http://127.0.0.1:"
        f"{CDP_PORT}/json"
    )

    for _ in range(60):

        try:

            response = requests.get(
                url,
                timeout=2
            )

            if response.ok:
                return

        except Exception:
            pass

        time.sleep(1)

    die(
        f"Edge CDP did not become available "
        f"on port {CDP_PORT}."
    )


def get_targets():

    response = requests.get(
        f"http://127.0.0.1:{CDP_PORT}/json",
        timeout=5
    )

    response.raise_for_status()

    return response.json()


# ============================================================
# TARGET DISCOVERY
# ============================================================

def find_drive_page(targets):

    for target in targets:

        if target.get("type") != "page":
            continue

        url = target.get(
            "url",
            ""
        )

        if "drive.google.com/file/d/" in url:

            return target

    return None


def find_player_iframe(targets):

    for target in targets:

        if target.get("type") != "iframe":
            continue

        url = target.get(
            "url",
            ""
        )

        if "youtube.googleapis.com/embed" in url:

            return target

    return None


def wait_for_targets():

    drive_page = None
    iframe = None

    log(
        "Waiting for Drive page and player..."
    )

    for _ in range(120):

        targets = get_targets()

        if not drive_page:

            drive_page = find_drive_page(
                targets
            )

        if not iframe:

            iframe = find_player_iframe(
                targets
            )

        if drive_page:

            log()
            log("Drive page detected:")
            log(
                drive_page.get(
                    "url",
                    ""
                )
            )

        if iframe:

            log()
            log("Drive player iframe detected:")
            log(
                iframe.get(
                    "url",
                    ""
                )
            )

        if drive_page and iframe:

            return drive_page, iframe

        time.sleep(1)

    die(
        "Could not find Drive page/player."
    )


# ============================================================
# SAVE RESULTS
# ============================================================

def save_results():

    with captured_lock:

        data = list(
            captured
        )

    result = {
        "drive_url":
            DRIVE_URL,

        "captured_at":
            time.time(),

        "capture_seconds":
            CAPTURE_SECONDS,

        "total_events":
            len(data),

        "video_itag":
            "136",

        "audio_itag":
            "140",

        "requests":
            data
    }

    with open(
        OUTPUT_JSON,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            result,
            f,
            indent=4,
            ensure_ascii=False
        )

    return data


# ============================================================
# SUMMARY
# ============================================================

def print_summary():

    with captured_lock:

        data = list(
            captured
        )

    video = [
        x for x in data
        if x.get("itag") == "136"
    ]

    audio = [
        x for x in data
        if x.get("itag") == "140"
    ]

    log()
    log()
    log("============================================================")
    log("CAPTURE SUMMARY")
    log("============================================================")

    log()
    log(
        f"Total captured events: {len(data)}"
    )

    log(
        f"itag=136 events: {len(video)}"
    )

    log(
        f"itag=140 events: {len(audio)}"
    )

    log()

    log("VIDEO REQUESTS:")
    log()

    for item in video:

        response = item.get(
            "response",
            {}
        )

        log(
            f"  #{item.get('sequence')} "
            f"range={item.get('url_range')} "
            f"rn={item.get('request_number')} "
            f"rbuf={item.get('request_buffer')} "
            f"ump={item.get('ump')} "
            f"status={response.get('status')} "
            f"mime={response.get('mime_type')} "
            f"encoded={item.get('loading_finished_encoded_data_length')}"
        )

    log()

    log("AUDIO REQUESTS:")
    log()

    for item in audio:

        response = item.get(
            "response",
            {}
        )

        log(
            f"  #{item.get('sequence')} "
            f"range={item.get('url_range')} "
            f"rn={item.get('request_number')} "
            f"rbuf={item.get('request_buffer')} "
            f"ump={item.get('ump')} "
            f"status={response.get('status')} "
            f"mime={response.get('mime_type')} "
            f"encoded={item.get('loading_finished_encoded_data_length')}"
        )

    log()

    log("============================================================")
    log("CAPTURE COMPLETE")
    log("============================================================")
    log()

    log(
        "Full capture saved to:"
    )

    log(
        OUTPUT_JSON
    )

    log()


# ============================================================
# AUTO CAPTURE TIMER
# ============================================================

def capture_timer():

    log()
    log(
        f"Automatic capture duration: "
        f"{CAPTURE_SECONDS} seconds."
    )

    log(
        "The script will stop automatically."
    )

    log()

    for remaining in range(
        CAPTURE_SECONDS,
        0,
        -1
    ):

        if stop_capture.is_set():
            return

        if remaining <= 10 or remaining % 10 == 0:

            log(
                f"Capture time remaining: "
                f"{remaining}s"
            )

        time.sleep(1)

    log()
    log(
        "Capture duration reached."
    )

    stop_capture.set()


# ============================================================
# MAIN
# ============================================================

def main():

    if not os.path.exists(
        EDGE_PATH
    ):
        die(
            "Microsoft Edge not found:\n"
            + EDGE_PATH
        )

    # --------------------------------------------------------
    # Remove previous capture
    # --------------------------------------------------------

    if os.path.exists(
        OUTPUT_JSON
    ):

        os.remove(
            OUTPUT_JSON
        )

    # --------------------------------------------------------
    # Launch
    # --------------------------------------------------------

    launch_edge()

    wait_for_cdp()

    time.sleep(3)

    # --------------------------------------------------------
    # Find targets
    # --------------------------------------------------------

    drive_page, iframe = (
        wait_for_targets()
    )

    # --------------------------------------------------------
    # Start listeners
    # --------------------------------------------------------

    threads = []

    for name, target in (
        ("DRIVE PAGE", drive_page),
        ("PLAYER IFRAME", iframe)
    ):

        thread = threading.Thread(
            target=network_listener,
            args=(
                name,
                target
            ),
            daemon=True
        )

        threads.append(
            thread
        )

        thread.start()

    # --------------------------------------------------------
    # User starts playback
    # --------------------------------------------------------

    log()
    log("============================================================")
    log("CAPTURE IS ACTIVE")
    log("============================================================")
    log()

    log("Press PLAY in the Drive player.")
    log()

    log(
        f"Once playback starts, capture will run "
        f"for {CAPTURE_SECONDS} seconds."
    )

    log()

    log(
        "DO NOT press Enter."
    )

    log(
        "DO NOT press Ctrl+C."
    )

    log()

    # --------------------------------------------------------
    # Start automatic timer
    # --------------------------------------------------------

    timer_thread = threading.Thread(
        target=capture_timer,
        daemon=True
    )

    timer_thread.start()

    # --------------------------------------------------------
    # Wait for automatic completion
    # --------------------------------------------------------

    while not stop_capture.is_set():

        time.sleep(0.5)

    # --------------------------------------------------------
    # Give CDP listeners time to finish
    # --------------------------------------------------------

    time.sleep(2)

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    save_results()

    print_summary()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    main()

