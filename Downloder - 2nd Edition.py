import json
import requests
from pathlib import Path
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

PROXY = "http://127.0.0.1:10808"

BOOK_IDS = range(1, 7)

BASE = "https://www.essentialenglish.review/apps-data"

OUTPUT_DIR = Path("output/2nd-edition")

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


def parse_reading(reading_html):
    """
    Extracts word entries and exercise media URLs from the reading HTML field.

    Returns a tuple of (words, exercise_image_src, exercise_audio_src):
      - words: list of dicts with keys: word, image, pronunciation, meaning, example
      - exercise_image_src: absolute path from <img class="img-app-small"> src attribute
      - exercise_audio_src: absolute path from <audio> src attribute
    """

    words = []
    exercise_image_src = ""
    exercise_audio_src = ""

    if not reading_html:
        return words, exercise_image_src, exercise_audio_src

    soup = BeautifulSoup(reading_html, "html.parser")

    for li in soup.find_all("li", attrs={"word": True}):

        word = li.get("word", "").strip()
        image = li.get("img", "").strip()
        pronunciation = li.get("pro", "").strip()

        divs = li.find_all("div", recursive=False)

        meaning = divs[0].get_text(separator=" ", strip=True) if len(divs) > 0 else ""
        example = divs[1].get_text(separator=" ", strip=True) if len(divs) > 1 else ""

        words.append({
            "word": word,
            "image": image,
            "pronunciation": pronunciation,
            "meaning": meaning,
            "example": example,
        })

    img_tag = soup.find("img", class_="img-app-small")
    if img_tag:
        exercise_image_src = img_tag.get("src", "").strip()

    audio_tag = soup.find("audio")
    if audio_tag:
        exercise_audio_src = audio_tag.get("src", "").strip()

    return words, exercise_image_src, exercise_audio_src


def process_book(book_id):

    book = f"4000-essential-english-words-{book_id}-2nd-edition"

    print("\n====== BOOK", book_id, "======")

    json_url = f"{BASE}/{book}/data/data.json"

    book_dir = OUTPUT_DIR / f"book{book_id}"

    images_dir = book_dir / "images"

    audio_dir = book_dir / "audio"

    book_dir.mkdir(parents=True, exist_ok=True)

    text = fetch_json(json_url)

    (book_dir / "data.json").write_text(text, encoding="utf-8")

    data = json.loads(text)

    for unit_index, card in enumerate(data["flashcard"], start=1):

        unit_title = card.get("en", f"Unit {unit_index}")

        print(f"[UNIT {unit_index}] {unit_title}")

        words, exercise_image_src, exercise_audio_src = parse_reading(card.get("reading", ""))

        print(f"  Words found: {len(words)}")

        if exercise_image_src:
            filename = exercise_image_src.split("/")[-1]
            cover_url = f"https://www.essentialenglish.review{exercise_image_src}"
            download(cover_url, images_dir / filename)

        if exercise_audio_src:
            filename = exercise_audio_src.split("/")[-1]
            cover_audio_url = f"https://www.essentialenglish.review{exercise_audio_src}"
            download(cover_audio_url, audio_dir / filename)

        for word_entry in words:

            image_name = word_entry["image"]
            word = word_entry["word"]

            if image_name:
                img_url = f"{BASE}/{book}/data/wordlist/{image_name}"
                download(img_url, images_dir / image_name)

            sound_name = f"{word}.mp3"
            audio_url = f"{BASE}/{book}/data/wordlist/{sound_name}"
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