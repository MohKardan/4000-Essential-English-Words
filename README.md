# 4000 Essential English Words – Data Extractor

A robust Python-based downloader for extracting vocabulary data, images, and audio files from the **4000 Essential English Words** learning platform.

This tool automatically downloads structured vocabulary data (`data.json`) along with all associated **media assets (images and pronunciation audio)** for every unit and book.

---

## Data Source

All data is extracted from:
https://www.essentialenglish.review

This repository **does not contain the original dataset**. It only provides tools to download the publicly available resources. All intellectual property rights belong to the original publisher.

## Supported Edition

Currently supported:
✅ **4000 Essential English Words – First Edition**

Planned support:
🚧 **4000 Essential English Words – Second Edition** (coming soon)

## Features

- **Automated Extraction:** Downloads vocabulary metadata, images, and audio.
- **Robustness:** Built-in retry mechanisms for network failures.
- **Efficiency:** Streaming downloads to optimize memory usage and "skip existing" logic to prevent redundant downloads.
- **Organization:** Automatic creation of a structured `output/` directory for each book.
- **Proxy Support:** Configurable proxy settings for restricted network environments.

## Project Structure
```text
output/
book1/
data.json
images/
audio/
...
