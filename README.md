# Video Stream Downloader

## Overview

This project intercepts playback video requests and downloads the actual video content from stream requests.

## How It Works

1. **Python2 Script** — Captures each playback video request and detects the byte ranges of the stream segments.
2. **Python1 Script** — Uses the detected ranges to download the actual video from the stream requests.

## Usage

1. Run the Python2 script to detect and log the byte ranges from playback requests.
2. The Python1 script reads the updated range data and downloads the corresponding video segments.

## Requirements

- Python 2.x (for range detection)
- Python 1.x compatible interpreter or environment (for downloading)

## Project Structure
