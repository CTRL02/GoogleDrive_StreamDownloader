import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import requests
from playwright.async_api import async_playwright


DRIVE_URL = "https://drive.google.com/file/d/1NFAAtaFh2F585v4NNTN8acjn7TIo0ASi/view"

CDP_URL = "http://127.0.0.1:9222"

PROFILE_DIR = Path("edge-drive-profile")

VIDEO_ITAG = "136"
AUDIO_ITAG = "140"

VIDEO_OUTPUT = Path("video_720p.mp4")
AUDIO_OUTPUT = Path("audio.m4a")
FINAL_OUTPUT = Path("video_720p_final.mp4")

RANGE_SIZE = 2 * 1024 * 1024

EDGE_PATH = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"


def start_edge():
    if not Path(EDGE_PATH).exists():
        print(f"[!] Edge not found at:")
        print(f"    {EDGE_PATH}")
        print()
        print("Change EDGE_PATH at the top of the script.")
        sys.exit(1)

    print("[+] Starting Edge...")

    cmd = [
        EDGE_PATH,
        "--remote-debugging-port=9222",
        "--remote-allow-origins=http://127.0.0.1:9222",
        f"--user-data-dir={PROFILE_DIR.resolve()}",
        "--no-first-run",
        "--no-default-browser-check",
        DRIVE_URL,
    ]

    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_for_cdp(timeout=30):
    print("[+] Waiting for CDP...")

    start = time.time()

    while time.time() - start < timeout:
        try:
            r = requests.get(
                f"{CDP_URL}/json/version",
                timeout=2,
            )

            if r.ok:
                print("[+] CDP available")
                return True

        except Exception:
            pass

        time.sleep(0.5)

    return False


def query_param(url, name):
    try:
        q = parse_qs(urlparse(url).query)
        values = q.get(name)

        if not values:
            return None

        return values[0]

    except Exception:
        return None


def set_query_params(url, **changes):
    parsed = urlparse(url)

    query = parse_qs(
        parsed.query,
        keep_blank_values=True,
    )

    for key, value in changes.items():
        query[key] = [str(value)]

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(query, doseq=True),
            parsed.fragment,
        )
    )


def describe_url(url):
    return {
        "itag": query_param(url, "itag"),
        "mime": query_param(url, "mime"),
        "clen": query_param(url, "clen"),
        "dur": query_param(url, "dur"),
        "range": query_param(url, "range"),
        "ump": query_param(url, "ump"),
        "host": urlparse(url).netloc,
    }


def read_varint(data, offset):
    if offset >= len(data):
        return None, offset, False

    first = data[offset]

    if (first & 0x80) == 0:
        size = 1
    elif (first & 0xC0) == 0x80:
        size = 2
    elif (first & 0xE0) == 0xC0:
        size = 3
    elif (first & 0xF0) == 0xE0:
        size = 4
    elif (first & 0xF8) == 0xF0:
        size = 5
    else:
        raise ValueError(
            f"Invalid UMP varint prefix: 0x{first:02x}"
        )

    if offset + size > len(data):
        return None, offset, False

    if size == 1:
        return first, offset + 1, True

    if size == 2:
        value = ((first & 0x3F) << 8) | data[offset + 1]

    elif size == 3:
        value = (
            (first & 0x1F)
            | (data[offset + 1] << 5)
            | (data[offset + 2] << 13)
        )

    elif size == 4:
        value = (
            (first & 0x0F)
            | (data[offset + 1] << 4)
            | (data[offset + 2] << 12)
            | (data[offset + 3] << 20)
        )

    else:
        value = (
            data[offset + 1]
            | (data[offset + 2] << 8)
            | (data[offset + 3] << 16)
            | (data[offset + 4] << 24)
        )

    return value, offset + size, True


class UMPDecoder:
    def __init__(self):
        self.partial_type = None
        self.partial_size = 0
        self.partial_data = bytearray()

    def reset_partial(self):
        self.partial_type = None
        self.partial_size = 0
        self.partial_data.clear()

    def feed(self, payload):
        """
        Decode one HTTP UMP response.

        Returns:

            {
                "media": [bytes, ...],
                "redirects": [str, ...],
                "headers": [bytes, ...],
                "end": bool,
                "parts": [...]
            }
        """

        pos = 0
        media = []
        redirects = []
        headers = []
        end = False
        parts = []

        while pos < len(payload):

            # Continuation of a previous partial part.
            if self.partial_type is not None:

                remaining = self.partial_size - len(
                    self.partial_data
                )

                take = min(
                    remaining,
                    len(payload) - pos,
                )

                self.partial_data.extend(
                    payload[pos:pos + take]
                )

                pos += take

                if len(self.partial_data) < self.partial_size:
                    parts.append(
                        (
                            self.partial_type,
                            len(self.partial_data),
                            "PARTIAL",
                        )
                    )

                    continue

                complete_type = self.partial_type
                complete_data = bytes(self.partial_data)

                self.reset_partial()

                parts.append(
                    (
                        complete_type,
                        len(complete_data),
                        "COMPLETE",
                    )
                )

                self.process_part(
                    complete_type,
                    complete_data,
                    media,
                    redirects,
                    headers,
                )

                if complete_type == 22:
                    end = True

                continue

            part_start = pos

            part_type, next_pos, ok = read_varint(
                payload,
                pos,
            )

            if not ok:
                break

            pos = next_pos

            part_size, next_pos, ok = read_varint(
                payload,
                pos,
            )

            if not ok:
                # Preserve the unconsumed bytes.
                break

            pos = next_pos

            available = len(payload) - pos

            if part_size > available:

                self.partial_type = part_type
                self.partial_size = part_size

                self.partial_data = bytearray(
                    payload[pos:]
                )

                pos = len(payload)

                parts.append(
                    (
                        part_type,
                        len(self.partial_data),
                        "PARTIAL",
                    )
                )

                continue

            part_data = payload[
                pos:pos + part_size
            ]

            pos += part_size

            parts.append(
                (
                    part_type,
                    part_size,
                    "COMPLETE",
                )
            )

            self.process_part(
                part_type,
                part_data,
                media,
                redirects,
                headers,
            )

            if part_type == 22:
                end = True

        return {
            "media": media,
            "redirects": redirects,
            "headers": headers,
            "end": end,
            "parts": parts,
        }

    def process_part(
        self,
        part_type,
        data,
        media,
        redirects,
        headers,
    ):
        if part_type == 20:
            headers.append(data)

        elif part_type == 21:
            # Current UMP format:
            #
            # type 21 MEDIA:
            #   header-id varint
            #   media bytes
            #
            # Older descriptions sometimes call the first byte
            # a null prefix. Do not blindly remove one byte here.
            #
            # First attempt to identify a UMP varint header-id.

            header_id, pos, ok = read_varint(
                data,
                0,
            )

            if ok and pos < len(data):
                media_bytes = data[pos:]
            else:
                media_bytes = data

            media.append(media_bytes)

        elif part_type == 22:
            pass

        elif part_type == 43:
            # SABR_REDIRECT.
            #
            # The payload contains a URL. Search for an
            # http/https URL rather than assuming a particular
            # protobuf field layout.

            url = extract_url(data)

            if url:
                redirects.append(url)

        else:
            # Ignore control/policy parts.
            pass


def extract_url(data):
    """
    Extract an http(s) URL from a type-43 SABR_REDIRECT payload.

    The exact protobuf layout can vary, so scan for a valid URL
    instead of assuming a fixed field number.
    """

    patterns = [
        rb"https://",
        rb"http://",
    ]

    candidates = []

    for marker in patterns:

        start = 0

        while True:
            index = data.find(
                marker,
                start,
            )

            if index == -1:
                break

            end = index

            while end < len(data):
                b = data[end]

                if b in (
                    0,
                    9,
                    10,
                    13,
                    32,
                    34,
                    39,
                ):
                    break

                end += 1

            candidate = data[index:end]

            try:
                text_value = candidate.decode(
                    "utf-8",
                    errors="strict",
                )

                if text_value.startswith(
                    (
                        "https://",
                        "http://",
                    )
                ):
                    candidates.append(
                        text_value
                    )

            except UnicodeDecodeError:
                pass

            start = index + len(marker)

    if not candidates:
        return None

    # Prefer googlevideo / drive.google hosts.
    candidates.sort(
        key=lambda x: (
            "googlevideo.com" not in x
            and "c.drive.google.com" not in x,
            len(x),
        )
    )

    return candidates[0]


def clean_redirect_url(url):
    """
    Redirect URLs sometimes arrive with protobuf/string
    escaping. Clean only obvious surrounding bytes.
    """

    url = url.strip()

    url = url.rstrip(
        "\x00\r\n\t \"'"
    )

    return url


async def browser_fetch(page, url):
    """
    Execute fetch() inside the Drive player iframe so the
    browser supplies the same authenticated/cookie context.
    """

    result = await page.evaluate(
        """
        async (url) => {
            try {
                const response = await fetch(url, {
                    method: "GET",
                    credentials: "include",
                    cache: "no-store"
                });

                const buffer =
                    await response.arrayBuffer();

                const bytes =
                    new Uint8Array(buffer);

                let binary = "";

                const CHUNK = 0x8000;

                for (
                    let i = 0;
                    i < bytes.length;
                    i += CHUNK
                ) {
                    binary += String.fromCharCode(
                        ...bytes.subarray(
                            i,
                            Math.min(
                                i + CHUNK,
                                bytes.length
                            )
                        )
                    );
                }

                return {
                    ok: response.ok,
                    status: response.status,
                    type:
                        response.headers.get(
                            "content-type"
                        ) || "",
                    length: bytes.length,
                    body: btoa(binary)
                };

            } catch (e) {
                return {
                    ok: false,
                    status: 0,
                    type: "",
                    length: 0,
                    body: "",
                    error: String(e)
                };
            }
        }
        """,
        url,
    )

    if not result.get("body"):
        raise RuntimeError(
            f"Browser fetch failed: {result}"
        )

    raw = base64.b64decode(
        result["body"]
    )

    return result, raw


async def discover_streams(context):
    """
    Discover actual signed media URLs from the player.
    """

    streams = {}

    pages = context.pages

    drive_page = None
    player_page = None

    for p in pages:

        try:
            url = p.url

            if "drive.google.com" in url:
                drive_page = p

            if "youtube.googleapis.com/embed" in url:
                player_page = p

        except Exception:
            pass

    if drive_page is None:
        drive_page = pages[0]

    print(
        f"[+] Drive page: {drive_page.url}"
    )

    print("[+] Looking for player iframe...")

    for _ in range(60):

        for p in context.pages:

            try:

                if (
                    "youtube.googleapis.com/embed"
                    in p.url
                ):
                    player_page = p
                    break

            except Exception:
                pass

        if player_page:
            break

        await asyncio.sleep(0.5)

    if not player_page:

        print(
            "[!] Player iframe was not found."
        )

        print(
            "[!] Open the Drive video manually "
            "and make sure the preview starts."
        )

        return None, None, drive_page

    print(
        f"[+] Player page: {player_page.url}"
    )

    print(
        "[+] Monitoring network requests..."
    )

    async def inspect_response(response):

        try:

            url = response.url

            itag = query_param(
                url,
                "itag",
            )

            mime = query_param(
                url,
                "mime",
            )

            if itag not in (
                VIDEO_ITAG,
                AUDIO_ITAG,
            ):
                return

            if (
                "videoplayback" not in url
                and "googlevideo" not in url
            ):
                return

            if itag not in streams:

                streams[itag] = url

                info = describe_url(url)

                print()
                print(
                    f"[+] Found itag={itag}"
                )
                print(
                    f"    mime = {info['mime']}"
                )
                print(
                    f"    clen = {info['clen']}"
                )
                print(
                    f"    dur  = {info['dur']}"
                )
                print(
                    f"    ump  = {info['ump']}"
                )
                print(
                    f"    host = {info['host']}"
                )

        except Exception:
            pass

    player_page.on(
        "response",
        inspect_response,
    )

    drive_page.on(
        "response",
        inspect_response,
    )

    print(
        "[+] Waiting for media requests..."
    )

    # Give Drive/player time to request streams.
    for i in range(60):

        if (
            VIDEO_ITAG in streams
            and AUDIO_ITAG in streams
        ):
            break

        await asyncio.sleep(0.5)

        if i % 5 == 0:
            print(
                f"    video={'yes' if VIDEO_ITAG in streams else 'no'} "
                f"audio={'yes' if AUDIO_ITAG in streams else 'no'}"
            )

    return (
        streams.get(VIDEO_ITAG),
        streams.get(AUDIO_ITAG),
        player_page,
    )


def make_range_url(base_url, start, end):
    return set_query_params(
        base_url,
        range=f"{start}-{end}",
        rn=int(time.time() * 1000) % 1000000,
        rbuf=16384,
        ump=1,
    )


def is_probably_mp4(data):
    if not data:
        return False

    return (
        b"ftyp" in data[:128]
        or b"moov" in data[:256]
        or b"moof" in data[:256]
        or b"mdat" in data[:256]
    )


def find_mp4_boxes(data):
    """
    Extract complete ISO-BMFF boxes from a buffer.

    Returns:
        list[(box_type, bytes)]
        remaining bytes
    """

    boxes = []

    pos = 0

    while True:

        if len(data) - pos < 8:
            break

        size = int.from_bytes(
            data[pos:pos + 4],
            "big",
        )

        box_type = data[
            pos + 4:
            pos + 8
        ]

        header_size = 8

        if size == 1:

            if len(data) - pos < 16:
                break

            size = int.from_bytes(
                data[pos + 8:
                     pos + 16],
                "big",
            )

            header_size = 16

        elif size == 0:

            # Box extends to end of file.
            # Do not emit until finalization.
            break

        if size < header_size:
            break

        if pos + size > len(data):
            break

        box = data[
            pos:
            pos + size
        ]

        boxes.append(
            (
                box_type.decode(
                    "latin1"
                ),
                box,
            )
        )

        pos += size

    return boxes, data[pos:]


def append_mp4_media(
    state,
    media_bytes,
    output_file,
):
    """
    Add media bytes to a persistent MP4 parser buffer.
    """

    state["buffer"].extend(
        media_bytes
    )

    boxes, remainder = find_mp4_boxes(
        bytes(state["buffer"])
    )

    state["buffer"] = bytearray(
        remainder
    )

    for box_type, box in boxes:

        output_file.write(box)

        state["bytes_written"] += len(box)
        state["boxes"] += 1

        if box_type == "mdat":
            state["mdat_bytes"] += len(box) - 8

        if box_type == "moov":
            state["has_moov"] = True

        if box_type == "ftyp":
            state["has_ftyp"] = True

        if box_type == "moof":
            state["moof_count"] += 1


async def download_stream(
    page,
    base_url,
    name,
    output_path,
):
    clen_text = query_param(
        base_url,
        "clen",
    )

    if not clen_text:
        raise RuntimeError(
            f"{name}: no clen in stream URL"
        )

    total = int(clen_text)

    print()
    print(
        "=" * 72
    )
    print(
        f"[+] {name} DOWNLOAD"
    )
    print(
        f"    total source bytes = {total:,}"
    )
    print(
        f"    output             = {output_path}"
    )
    print(
        "=" * 72
    )

    decoder = UMPDecoder()

    state = {
        "buffer": bytearray(),
        "bytes_written": 0,
        "boxes": 0,
        "mdat_bytes": 0,
        "moof_count": 0,
        "has_ftyp": False,
        "has_moov": False,
    }

    current_url = base_url

    source_position = 0

    redirect_count = 0

    media_parts = 0

    response_count = 0

    with open(
        output_path,
        "wb",
    ) as output:

        while source_position < total:

            end = min(
                source_position
                + RANGE_SIZE
                - 1,
                total - 1,
            )

            request_url = make_range_url(
                current_url,
                source_position,
                end,
            )

            print()
            print(
                f"[{name}] "
                f"{source_position:,}-"
                f"{end:,} "
                f"({end-source_position+1:,} bytes)"
            )

            meta, raw = await browser_fetch(
                page,
                request_url,
            )

            response_count += 1

            print(
                f"    HTTP={meta.get('status')} "
                f"type={meta.get('type')} "
                f"received={len(raw):,}"
            )

            if not raw:
                raise RuntimeError(
                    f"{name}: empty response"
                )

            decoded = decoder.feed(raw)

            if decoded["parts"]:

                descriptions = []

                for (
                    ptype,
                    psize,
                    status,
                ) in decoded["parts"]:

                    descriptions.append(
                        f"{ptype}:{psize}:{status}"
                    )

                print(
                    "    UMP parts: "
                    + ", ".join(
                        descriptions[:12]
                    )
                )

                if len(
                    descriptions
                ) > 12:
                    print(
                        f"    ... "
                        f"+{len(descriptions)-12} parts"
                    )

            # Handle SABR redirects.
            if decoded["redirects"]:

                for redirect in decoded[
                    "redirects"
                ]:

                    redirect = clean_redirect_url(
                        redirect
                    )

                    if redirect.startswith(
                        (
                            "http://",
                            "https://",
                        )
                    ):

                        redirect_count += 1

                        print()
                        print(
                            "[REDIRECT]"
                        )
                        print(
                            f"    {redirect}"
                        )

                        current_url = redirect

                        break

            media_this_response = 0

            for media in decoded["media"]:

                if not media:
                    continue

                media_parts += 1

                media_this_response += len(
                    media
                )

                append_mp4_media(
                    state,
                    media,
                    output,
                )

            if media_this_response:

                print(
                    f"    decoded media: "
                    f"{media_this_response:,}"
                )

                print(
                    f"    MP4 written: "
                    f"{state['bytes_written']:,}"
                )

            else:

                print(
                    "    decoded media: 0"
                )

            progress = (
                (end + 1)
                / total
                * 100
            )

            print(
                f"    progress: "
                f"{progress:6.2f}%"
                f" | MP4="
                f"{state['bytes_written']:,}"
                f" | tail="
                f"{len(state['buffer']):,}"
            )

            source_position = end + 1

    print()
    print(
        f"[+] {name} source bytes requested: "
        f"{total:,}"
    )

    print(
        f"[+] {name} decoded media parts: "
        f"{media_parts:,}"
    )

    print(
        f"[+] {name} MP4 bytes written: "
        f"{state['bytes_written']:,}"
    )

    print(
        f"[+] {name} MP4 boxes: "
        f"{state['boxes']:,}"
    )

    print(
        f"[+] {name} moof boxes: "
        f"{state['moof_count']:,}"
    )

    print(
        f"[+] {name} mdat payload bytes: "
        f"{state['mdat_bytes']:,}"
    )

    print(
        f"[+] {name} trailing buffer: "
        f"{len(state['buffer']):,}"
    )

    print(
        f"[+] {name} redirects: "
        f"{redirect_count}"
    )

    if not state["has_ftyp"]:
        print(
            f"[!] {name}: no ftyp box detected"
        )

    if not state["has_moov"]:
        print(
            f"[!] {name}: no moov box detected"
        )

    if state["moof_count"] == 0:
        print(
            f"[!] {name}: no moof boxes detected"
        )

    if state["bytes_written"] == 0:
        raise RuntimeError(
            f"{name}: zero MP4 bytes reconstructed"
        )

    if state["buffer"]:
        print()
        print(
            f"[!] {name}: "
            f"{len(state['buffer']):,} "
            f"bytes remain in MP4 parser buffer."
        )

    return state


def run_ffprobe(path):
    print()
    print(
        "=" * 72
    )
    print(
        f"[+] ffprobe: {path}"
    )
    print(
        "=" * 72
    )

    result = subprocess.run(
        [
            FFPROBE,
            "-hide_banner",
            "-v",
            "error",
            "-show_entries",
            "format=duration,size,bit_rate",
            "-show_entries",
            "stream=index,codec_name,codec_type,width,height,r_frame_rate,avg_frame_rate,duration,bit_rate",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
    )

    print(
        result.stdout
    )

    if result.returncode != 0:
        print(
            result.stderr
        )

    return result.returncode == 0


def mux(video, audio, output):
    print()
    print(
        "=" * 72
    )
    print(
        "[+] MUXING"
    )
    print(
        "=" * 72
    )

    cmd = [
        FFMPEG,
        "-y",

        "-i",
        str(video),

        "-i",
        str(audio),

        "-map",
        "0:v:0",

        "-map",
        "1:a:0",

        "-c:v",
        "copy",

        "-c:a",
        "copy",

        "-movflags",
        "+faststart",

        str(output),
    ]

    print(
        " ".join(
            f'"{x}"' if " " in x else x
            for x in cmd
        )
    )

    result = subprocess.run(
        cmd
    )

    if result.returncode != 0:
        raise RuntimeError(
            "ffmpeg mux failed"
        )

    print()
    print(
        f"[+] Final file:"
    )
    print(
        f"    {output.resolve()}"
    )


async def main():
    print()
    print(
        "=" * 72
    )
    print(
        "GOOGLE DRIVE 720p UMP DOWNLOADER"
    )
    print(
        "=" * 72
    )

    if not shutil.which("ffmpeg"):
        print(
            "[!] ffmpeg is not in PATH."
        )
        sys.exit(1)

    if not shutil.which("ffprobe"):
        print(
            "[!] ffprobe is not in PATH."
        )
        sys.exit(1)

    if not wait_for_cdp():
        print(
            "[!] CDP was not available."
        )
        print(
            "[!] Edge may not have started."
        )
        sys.exit(1)

    async with async_playwright() as p:

        browser = await p.chromium.connect_over_cdp(
            CDP_URL
        )

        contexts = browser.contexts

        if not contexts:
            raise RuntimeError(
                "No browser context found"
            )

        context = contexts[0]

        video_url = None
        audio_url = None
        player_page = None

        # First inspect existing pages.
        for page in context.pages:

            print(
                f"[PAGE] {page.url}"
            )

            if (
                "youtube.googleapis.com/embed"
                in page.url
            ):
                player_page = page

        video_url, audio_url, player_page = (
            await discover_streams(
                context
            )
        )

        if not video_url:

            print()
            print(
                "[!] Did not discover itag=136."
            )
            print(
                "[!] Make sure the Drive preview "
                "has loaded and started."
            )

            await browser.close()

            return

        if not audio_url:

            print()
            print(
                "[!] Did not discover itag=140."
            )
            print(
                "[!] Video was found but audio "
                "was not captured."
            )

            await browser.close()

            return

        print()
        print(
            "[+] STREAMS"
        )

        print(
            f"    VIDEO:"
        )
        print(
            f"    {video_url}"
        )

        print()
        print(
            f"    AUDIO:"
        )
        print(
            f"    {audio_url}"
        )

        print()

        # ------------------------------------------------------------
        # VIDEO
        # ------------------------------------------------------------

        video_state = await download_stream(
            player_page,
            video_url,
            "VIDEO",
            VIDEO_OUTPUT,
        )

        # ------------------------------------------------------------
        # AUDIO
        # ------------------------------------------------------------

        audio_state = await download_stream(
            player_page,
            audio_url,
            "AUDIO",
            AUDIO_OUTPUT,
        )

        # ------------------------------------------------------------
        # VALIDATE
        # ------------------------------------------------------------

        print()
        print(
            "=" * 72
        )
        print(
            "VALIDATION"
        )
        print(
            "=" * 72
        )

        run_ffprobe(
            VIDEO_OUTPUT
        )

        run_ffprobe(
            AUDIO_OUTPUT
        )

        # ------------------------------------------------------------
        # MUX
        # ------------------------------------------------------------

        mux(
            VIDEO_OUTPUT,
            AUDIO_OUTPUT,
            FINAL_OUTPUT,
        )

        # ------------------------------------------------------------
        # FINAL VALIDATION
        # ------------------------------------------------------------

        run_ffprobe(
            FINAL_OUTPUT
        )

        print()
        print(
            "=" * 72
        )
        print(
            "[+] COMPLETE"
        )
        print(
            "=" * 72
        )

        print(
            f"Video: {VIDEO_OUTPUT.resolve()}"
        )

        print(
            f"Audio: {AUDIO_OUTPUT.resolve()}"
        )

        print(
            f"Final: {FINAL_OUTPUT.resolve()}"
        )

        await browser.close()


if __name__ == "__main__":
    asyncio.run(
        main()
    )