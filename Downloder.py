import json
import requests
from pathlib import Path
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

PROXY = "http://127.0.0.1:10808"

BOOK_IDS = range(1, 7)

BASE = "https://www.essentialenglish.review/apps-data"

OUTPUT_DIR = Path("output")

proxies = {
    "http": PROXY,
    "https": PROXY,
}

session = requests.Session()

session.proxies.update(proxies)

session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*",
})

retry = Retry(
    total=5,
    backoff_factor=2,
    status_forcelist=[429, 500, 502, 503, 504],
)

adapter = HTTPAdapter(max_retries=retry)

session.mount("http://", adapter)
session.mount("https://", adapter)


def fetch_json(url):

    print("[DEBUG] Fetching JSON:", url)

    r = session.get(url, timeout=60)

    if r.status_code != 200:
        raise RuntimeError(f"Failed to fetch {url}")

    return r.content.decode("utf-8-sig")


def download(url, path):

    if path.exists():
        print("[SKIP]", path)
        return

    path.parent.mkdir(parents=True, exist_ok=True)

    try:

        with session.get(url, stream=True, timeout=60) as r:

            if r.status_code != 200:
                print("[FAIL]", url)
                return

            with open(path, "wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)

        print("[OK]", path)

    except Exception as e:

        print("[ERROR]", url, e)


def process_book(book_id):

    book = f"4000-essential-english-words-{book_id}"

    print("\n====== BOOK", book_id, "======")

    json_url = f"{BASE}/{book}/data/data.json"

    book_dir = OUTPUT_DIR / f"book{book_id}"

    images_dir = book_dir / "images"

    audio_dir = book_dir / "audio"

    book_dir.mkdir(parents=True, exist_ok=True)

    text = fetch_json(json_url)

    (book_dir / "data.json").write_text(text, encoding="utf-8")

    data = json.loads(text)

    for unit_index, unit in enumerate(data["flashcard"], start=1):

        print(f"[UNIT {unit_index}]")

        for word in unit["wordlist"]:

            image_name = word.get("image")
            sound_name = word.get("sound")

            if image_name:

                img_url = f"{BASE}/{book}/data/unit-{unit_index}/wordlist/{image_name}"

                download(img_url, images_dir / image_name)

            if sound_name:

                audio_url = f"{BASE}/{book}/data/unit-{unit_index}/wordlist/{sound_name}"

                download(audio_url, audio_dir / sound_name)


def main():

    OUTPUT_DIR.mkdir(exist_ok=True)

    for book in BOOK_IDS:

        try:

            process_book(book)

        except Exception as e:

            print("[BOOK FAILED]", book, e)


if __name__ == "__main__":

    main()
