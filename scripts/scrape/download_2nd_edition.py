import json
import re
import time
import requests
from pathlib import Path
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROXY = "http://127.0.0.1:10808"

SITE = "https://www.essentialenglish.review"

BOOK_IDS = range(1, 7)

OUTPUT_DIR = Path("data/2nd-edition")

SLEEP_BETWEEN_UNITS = 1

# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------

proxies = {
    "http": PROXY,
    "https": PROXY,
}

session = requests.Session()

session.proxies.update(proxies)

session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*",
})

retry = Retry(
    total=5,
    backoff_factor=2,
    status_forcelist=[429, 500, 502, 503, 504],
)

adapter = HTTPAdapter(max_retries=retry)

session.mount("http://", adapter)
session.mount("https://", adapter)

# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def fetch_html(url):

    print("[FETCH]", url)

    r = session.get(url, timeout=60)

    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} for {url}")

    return r.text


def download(url, path):

    if path.exists():
        print("[SKIP]", path)
        return

    path.parent.mkdir(parents=True, exist_ok=True)

    try:

        with session.get(url, stream=True, timeout=60) as r:

            if r.status_code not in (200, 206):
                print("[FAIL]", url)
                return

            with open(path, "wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)

        print("[OK]", path)

    except Exception as e:

        print("[ERROR]", url, e)

# ---------------------------------------------------------------------------
# Scraping helpers
# ---------------------------------------------------------------------------

def get_unit_slugs(book_id):
    """
    Fetches the book index page and returns an ordered list of unit page paths.
    Unit nav links follow: /book/{book-slug}/unit-{n}-{story-title}#{n-1}
    The hash fragment is stripped; duplicates are removed while preserving order.
    """

    index_url = f"{SITE}/4000-essential-english-words-{book_id}-2nd-edition/"

    html = fetch_html(index_url)

    soup = BeautifulSoup(html, "html.parser")

    prefix = f"/book/4000-essential-english-words-{book_id}-2nd-edition/unit-"

    slugs = []
    seen = set()

    for a in soup.find_all("a", href=True):

        href = a["href"]

        if href.startswith(prefix) and "#" in href:

            slug = href.split("#")[0]

            if slug not in seen:
                seen.add(slug)
                slugs.append(slug)

    return slugs


def parse_unit_page(html, book_id):
    """
    Extracts all data needed to build one flashcard entry from a unit page.

    Returns a dict matching the data.json flashcard schema:
        image   - story image filename (extracted from <img class="img-app-small"> src)
        en      - unit title (from <title> tag)
        desc    - empty string (not present on unit pages)
        reading - inner HTML of <div class="page-content"> with story src paths
                  rewritten to use the correct book number
    """

    soup = BeautifulSoup(html, "html.parser")

    # -- en: unit title from <title> tag
    title_tag = soup.find("title")
    en = title_tag.get_text(strip=True) if title_tag else ""

    # -- desc: not available on unit pages
    desc = ""

    # -- reading: inner HTML of page-content div, minified to match the
    # compact single-line format of the original data.json
    page_content = soup.find("div", class_="page-content")

    if not page_content:
        return {"image": "", "en": en, "desc": desc, "reading": ""}

    # Strip newlines and inter-tag whitespace to produce a compact single-line
    # string, matching the format of the original data.json reading field.
    reading_html = re.sub(r"\n\s*", "", page_content.decode_contents())

    # Rewrite hardcoded book-1 src paths to the correct book number.
    # The site embeds /apps-data/4000-essential-english-words-1-2nd-edition/
    # in every unit page regardless of which book it belongs to.
    wrong_book_path = "/apps-data/4000-essential-english-words-1-2nd-edition/"
    correct_book_path = f"/apps-data/4000-essential-english-words-{book_id}-2nd-edition/"

    reading_html = reading_html.replace(wrong_book_path, correct_book_path)

    # -- image: story image filename from the corrected reading HTML
    reading_soup = BeautifulSoup(reading_html, "html.parser")

    img_tag = reading_soup.find("img", class_="img-app-small")
    image = img_tag["src"].split("/")[-1] if img_tag else ""

    return {
        "image": image,
        "en": en,
        "desc": desc,
        "reading": reading_html,
    }

# ---------------------------------------------------------------------------
# Media download helpers
# ---------------------------------------------------------------------------

def download_unit_media(reading_html, book, images_dir, audio_dir):
    """
    Downloads all media files referenced in a unit's reading HTML:
      - story image and story audio (from exercise/ path)
      - wordlist images and audio (from wordlist/ path)
    """

    soup = BeautifulSoup(reading_html, "html.parser")

    # Story image
    img_tag = soup.find("img", class_="img-app-small")
    if img_tag:
        src = img_tag.get("src", "")
        filename = src.split("/")[-1]
        download(f"{SITE}{src}", images_dir / filename)

    # Story audio
    audio_tag = soup.find("audio")
    if audio_tag:
        src = audio_tag.get("src", "")
        filename = src.split("/")[-1]
        download(f"{SITE}{src}", audio_dir / filename)

    # Wordlist images and audio
    for li in soup.find_all("li", attrs={"word": True}):

        word = li.get("word", "").strip()
        image = li.get("img", "").strip()

        if image:
            img_url = f"{SITE}/apps-data/{book}/data/wordlist/{image}"
            download(img_url, images_dir / image)

        if word:
            sound_name = f"{word}.mp3"
            audio_url = f"{SITE}/apps-data/{book}/data/wordlist/{sound_name}"
            download(audio_url, audio_dir / sound_name)

# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def process_book(book_id):

    book = f"4000-essential-english-words-{book_id}-2nd-edition"

    print(f"\n====== BOOK {book_id} ======")

    book_dir = OUTPUT_DIR / f"book{book_id}"
    images_dir = book_dir / "images"
    audio_dir = book_dir / "audio"

    book_dir.mkdir(parents=True, exist_ok=True)

    unit_slugs = get_unit_slugs(book_id)

    print(f"Units found: {len(unit_slugs)}")

    flashcards = []

    for unit_index, slug in enumerate(unit_slugs, start=1):

        unit_name = slug.split("/")[-1]

        print(f"\n[UNIT {unit_index}] {unit_name}")

        try:
            html = fetch_html(f"{SITE}{slug}")
        except Exception as e:
            print("[UNIT FAILED]", e)
            continue

        card = parse_unit_page(html, book_id)

        words_count = len(BeautifulSoup(card["reading"], "html.parser").find_all("li", attrs={"word": True}))
        print(f"  Words found: {words_count}")

        flashcards.append(card)

        download_unit_media(card["reading"], book, images_dir, audio_dir)

        time.sleep(SLEEP_BETWEEN_UNITS)

    # Save data.json in the same schema as the original
    data = {"flashcard": flashcards}

    data_json_path = book_dir / "data.json"
    data_json_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\n[SAVED] {data_json_path}  ({len(flashcards)} units)")


def main():

    OUTPUT_DIR.mkdir(exist_ok=True)

    for book_id in BOOK_IDS:

        try:
            process_book(book_id)
        except Exception as e:
            print("[BOOK FAILED]", book_id, e)


if __name__ == "__main__":

    main()