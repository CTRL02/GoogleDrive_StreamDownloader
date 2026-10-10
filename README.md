This project intercepts playback video requests and downloads the actual video content from stream requests.


1. **Python2 Script** — Captures each playback video request and detects the byte ranges of the stream segments.
2. **Python1 Script** — Uses the detected ranges to download the actual video from the stream requests.
