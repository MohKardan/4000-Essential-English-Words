#!/usr/bin/env python3
"""
add_hints_to_shared_deck.py
----------------------------
Fill in the empty "Hint" field of the shared "4000 EEW" Anki deck using the
contextual sentence that contains each word in the official 2nd Edition
story text (same sentence-extraction approach as anki_generator.py).

This script edits the collection in place: note ids and GUIDs are left
untouched, only the Hint slot of the matching notes' `flds` is replaced.
That keeps the notes stable for GUID-based Anki import, so importing the
output apkg into a profile that already studies this deck updates the Hint
field on the existing notes without resetting their review history.

Requirements:
    pip install zstandard bs4 spacy stanza --break-system-packages
    python -m spacy download en_core_web_lg

Usage (run from the repo root):
    python scripts/translate/add_hints_to_shared_deck.py \
        --input "dist/shared-deck/4000 Essential English Words (all books).apkg" \
        --output "dist/shared-deck/4000 Essential English Words (all books).hints.apkg" \
        --data-dir "data/2nd-edition" \
        --overwrite   # (optional) replace non-empty Hint fields too

Each note is matched to its book via the deck it belongs to (decks are named
"...::1.Book" .. "...::6.Book"), and its Hint is looked up in that book's
own word -> hint map (built from data/2nd-edition/bookN/data.json). This
also works unchanged for a single-book apkg (e.g. only book1's deck/data
present): every note simply resolves to book 1.
"""

import argparse
import json
import re
import shutil
import sqlite3
import sys
import time
import zipfile
from pathlib import Path

try:
    import zstandard
except ImportError:
    print("The 'zstandard' package is not installed. Run: pip install zstandard --break-system-packages")
    sys.exit(1)

# anki_generator.py lives in a sibling directory (scripts/generate/), not on
# sys.path by default -- add it explicitly rather than relying on cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "generate"))
from anki_generator import find_sentence_with_word_spaCy, nlp_spaCy, clean_text

FIELD_SEP = "\x1f"
NOTETYPE_NAME = "4000 EEW"
WORD_FIELD = "Word"
HINT_FIELD = "Hint"
NOT_FOUND = "Context not found."

# Max extra characters a token may have beyond the target word for the
# prefix fallback to accept it (covers suffixes like -ed, -ing, -s, -er).
PREFIX_FALLBACK_MAX_EXTRA_CHARS = 4

WORDLIST_ITEM_RE = re.compile(r'<li img="([^"]*)" pro="([^"]*)" word="([^"]*)">(.*?)</li>', re.DOTALL)
SECTION_MARKER_RE = re.compile(r'<p class="section-rotate">')


def _unicase(a: str, b: str) -> int:
    """Case-insensitive stand-in for Anki's custom 'unicase' SQLite collation."""
    la, lb = a.lower(), b.lower()
    return (la > lb) - (la < lb)


def connect(path) -> sqlite3.Connection:
    """sqlite3.connect() with Anki's 'unicase' collation registered.

    Newer (anki21b) collections declare text columns like notetypes.name
    with COLLATE unicase; without registering it, any query touching those
    columns raises "no such collation sequence: unicase".
    """
    conn = sqlite3.connect(path)
    conn.create_collation("unicase", _unicase)
    return conn


# ---------------------------------------------------------------------------
# 2nd Edition story parsing -> word -> hint sentence
# ---------------------------------------------------------------------------

def extract_story_html(reading_html: str) -> str:
    """
    The reading HTML for a unit contains, in order: the word list, the
    exercises, the story (starting at the only <h3> tag), then the Reading
    Comprehension exercise. This isolates the story portion so that hint
    sentences aren't picked from exercise text.
    """
    h3_match = re.search(r"<h3>", reading_html)
    if not h3_match:
        return ""
    marker_match = SECTION_MARKER_RE.search(reading_html, h3_match.end())
    end = marker_match.start() if marker_match else len(reading_html)
    return reading_html[h3_match.start():end]


def extract_wordlist(reading_html: str):
    """Returns a list of (img, pron, word, inner_html) tuples from the unit's <ul class="wordlist">."""
    return WORDLIST_ITEM_RE.findall(reading_html)


INNER_DIV_RE = re.compile(r"<div>(.*?)</div>", re.DOTALL)


def extract_own_example(inner_html: str):
    """
    The wordlist <li>'s own example sentence (its 2nd <div>; the 1st is the
    definition). Used as a final fallback when the target word never
    appears in the unit's story at all - e.g. "reverse" in Book 4 Unit 26 is
    only used in the wordlist's own definition/example, never in the
    narrative, so no story sentence can ever be found for it.
    """
    divs = INNER_DIV_RE.findall(inner_html)
    if len(divs) < 2:
        return None
    return clean_text(divs[1])


def find_sentence_with_word_prefix(html_story: str, target_word: str):
    """
    Last-resort fallback for when lemma-based matching (spaCy/Stanza) fails.

    Some inflected forms - participial adjectives like "concerned"/"excited",
    or words whose out-of-context POS guess is wrong like standalone "lower"
    being read as the comparative of "low" - never reduce to the target
    word's lemma. Instead, look for a whole token that starts with the
    target word and isn't much longer than it, which covers regular -ed/
    -ing/-s/-er suffixes without matching unrelated longer words.

    Also tries the regular "consonant + y" -> "ied" spelling change (e.g.
    dissatisfy -> dissatisfied), which a plain prefix check misses.
    """
    text = clean_text(html_story)
    doc = nlp_spaCy(text)
    target = target_word.lower()

    alt_target = None
    if len(target) > 1 and target.endswith("y") and target[-2] not in "aeiou":
        alt_target = target[:-1] + "i"

    for sent in doc.sents:
        for token in sent:
            tok = token.text.lower()
            if tok.startswith(target) and len(tok) - len(target) <= PREFIX_FALLBACK_MAX_EXTRA_CHARS:
                return sent.text.strip()
            if alt_target and tok.startswith(alt_target) and len(tok) - len(alt_target) <= PREFIX_FALLBACK_MAX_EXTRA_CHARS:
                return sent.text.strip()

    return None


def build_hint_map(data_json_path: Path) -> dict:
    with open(data_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    hint_map = {}
    for unit in data["flashcard"]:
        reading = unit.get("reading", "")
        story_html = extract_story_html(reading)
        if not story_html:
            print(f"[WARN] No story found for unit '{unit.get('en')}', skipping its words.")
            continue

        for _img, _pron, word, inner in extract_wordlist(reading):
            if not word:
                continue
            key = word.lower()
            if key in hint_map:
                print(f"[WARN] Word '{word}' appears in more than one unit; keeping first hint found.")
                continue
            hint = find_sentence_with_word_spaCy(story_html, word)
            if hint.strip() == NOT_FOUND:
                fallback = find_sentence_with_word_prefix(story_html, word)
                if fallback:
                    hint = fallback
                else:
                    own_example = extract_own_example(inner)
                    if own_example:
                        print(f"[INFO] '{word}' isn't used in the story; using its own example sentence as Hint.")
                        hint = own_example
                    else:
                        print(f"[WARN] No hint found for '{word}', even with fallbacks.")
            hint_map[key] = hint

    return hint_map


def discover_book_data_jsons(data_dir: Path) -> dict:
    """Returns {book_number: data.json path} for every data/2nd-edition/bookN/ found."""
    books = {}
    for path in sorted(data_dir.glob("book*/data.json")):
        m = re.match(r"book(\d+)$", path.parent.name)
        if m:
            books[int(m.group(1))] = path
    return books


def build_hint_maps(data_dir: Path) -> dict:
    """Returns {book_number: {word: hint}} for every book found under data_dir."""
    hint_maps = {}
    for book, data_json_path in discover_book_data_jsons(data_dir).items():
        print(f"Building hint map for book {book} from {data_json_path} ...")
        hint_maps[book] = build_hint_map(data_json_path)
        print(f"  -> {len(hint_maps[book])} hints.")
    return hint_maps


# ---------------------------------------------------------------------------
# apkg reading/writing
# ---------------------------------------------------------------------------

def load_collection(work_dir: Path):
    """
    Locate whichever collection file in the extracted apkg actually holds
    the notes (newer exports keep an empty legacy collection.anki2 stub
    alongside the real, zstd-compressed collection.anki21b). Returns
    (kind, sqlite_path) where kind is "anki21b" or "anki2".
    """
    candidates = []

    anki21b = work_dir / "collection.anki21b"
    if anki21b.exists():
        decompressed = zstandard.ZstdDecompressor().decompress(
            anki21b.read_bytes(), max_output_size=500 * 1024 * 1024
        )
        decoded_path = work_dir / "_collection21b_decoded.sqlite"
        decoded_path.write_bytes(decompressed)
        candidates.append(("anki21b", decoded_path))

    anki2 = work_dir / "collection.anki2"
    if anki2.exists():
        candidates.append(("anki2", anki2))

    if not candidates:
        raise FileNotFoundError("No collection.anki2 / collection.anki21b found inside the apkg.")

    best = None
    for kind, path in candidates:
        conn = connect(path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        finally:
            conn.close()
        if best is None or count > best[2]:
            best = (kind, path, count)

    return best[0], best[1]


def get_notetype_fields(conn: sqlite3.Connection, kind: str, notetype_name: str):
    """Returns (mid, {field_name: index}) for the given notetype name, or (None, None)."""
    cur = conn.cursor()

    if kind == "anki21b":
        row = cur.execute("SELECT id FROM notetypes WHERE name=?", (notetype_name,)).fetchone()
        if not row:
            return None, None
        ntid = row[0]
        rows = cur.execute("SELECT ord, name FROM fields WHERE ntid=?", (ntid,)).fetchall()
        return ntid, {name: ord_ for ord_, name in rows}

    row = cur.execute("SELECT models FROM col").fetchone()
    models = json.loads(row[0])
    for mid, model in models.items():
        if model.get("name") == notetype_name:
            return mid, {f["name"]: i for i, f in enumerate(model["flds"])}
    return None, None


DECK_BOOK_RE = re.compile(r"(\d+)\.Book$")


def get_note_books(conn: sqlite3.Connection, mid: int) -> dict:
    """
    Returns {note_id: book_number} by resolving each note's card -> deck,
    where decks are named "...::N.Book" (stored internally with \x1f as the
    hierarchy separator). Notes whose deck doesn't match that pattern (e.g.
    a shared "Extra" deck) are omitted.
    """
    note_books = {}
    rows = conn.execute(
        """
        SELECT n.id, d.name FROM notes n
        JOIN cards c ON c.nid = n.id
        JOIN decks d ON d.id = c.did
        WHERE n.mid = ?
        """,
        (mid,),
    ).fetchall()

    for note_id, deck_name in rows:
        last_component = deck_name.split(FIELD_SEP)[-1]
        m = DECK_BOOK_RE.match(last_component)
        if m:
            note_books[note_id] = int(m.group(1))

    return note_books


def apply_hints(sqlite_path: Path, kind: str, hint_maps: dict, notetype_name: str, overwrite: bool):
    conn = connect(sqlite_path)
    try:
        mid, field_idx = get_notetype_fields(conn, kind, notetype_name)

        if mid is None:
            raise ValueError(f"Notetype '{notetype_name}' not found in this collection.")
        if WORD_FIELD not in field_idx or HINT_FIELD not in field_idx:
            raise ValueError(f"Notetype '{notetype_name}' has no '{WORD_FIELD}'/'{HINT_FIELD}' field.")

        word_idx = field_idx[WORD_FIELD]
        hint_idx = field_idx[HINT_FIELD]

        # notes.mid is always stored as an integer regardless of schema kind;
        # only the notetypes/models lookup differs between anki21b and anki2.
        mid_int = int(mid)
        rows = conn.execute("SELECT id, flds FROM notes WHERE mid=?", (mid_int,)).fetchall()
        note_books = get_note_books(conn, mid_int)

        updated, skipped, missing, no_deck = 0, 0, [], 0

        for note_id, flds in rows:
            parts = flds.split(FIELD_SEP)
            word = parts[word_idx].strip()
            current_hint = parts[hint_idx].strip()

            if current_hint and not overwrite:
                skipped += 1
                continue

            book = note_books.get(note_id)
            if book is None or book not in hint_maps:
                no_deck += 1
                continue

            hint = hint_maps[book].get(word.lower())
            if hint is None:
                missing.append(f"{word} (book {book})")
                continue

            parts[hint_idx] = hint
            # Anki's importer matches by GUID but only overwrites an existing
            # note's fields if the incoming `mod` is newer than the local
            # note's `mod`. Leaving `mod` untouched (as this script did before)
            # makes the update a silent no-op on any collection that already
            # has these notes. usn=-1 is the standard "modified locally,
            # needs sync" marker Anki itself uses for edited notes.
            conn.execute(
                "UPDATE notes SET flds=?, mod=?, usn=-1 WHERE id=?",
                (FIELD_SEP.join(parts), int(time.time()), note_id),
            )
            updated += 1

        conn.commit()
    finally:
        conn.close()

    print(f"Done: {updated} notes updated, {skipped} skipped (already had a hint), "
          f"{len(missing)} had no match, {no_deck} not resolved to a known book/deck.")
    if missing:
        print("[WARN] No hint found for:", ", ".join(missing))


def repackage(work_dir: Path, kind: str, sqlite_path: Path, output_path: Path):
    if kind == "anki21b":
        compressed = zstandard.ZstdCompressor().compress(sqlite_path.read_bytes())
        (work_dir / "collection.anki21b").write_bytes(compressed)

    if output_path.exists():
        output_path.unlink()

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in work_dir.rglob("*"):
            if not item.is_file():
                continue
            if item.name in ("_collection21b_decoded.sqlite",):
                continue
            if item.suffix in (".wal", ".shm", "-journal"):
                continue
            zf.write(item, item.relative_to(work_dir))

    print(f"Output file saved: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Add story-based Hint sentences to the shared 4000 EEW deck")
    parser.add_argument("--input", required=True, help="Path to the source apkg")
    parser.add_argument("--output", required=True, help="Path to write the updated apkg")
    parser.add_argument("--data-dir", required=True,
                         help="Directory containing book1/data.json .. book6/data.json (e.g. data/2nd-edition)")
    parser.add_argument("--notetype", default=NOTETYPE_NAME, help="Note type name to update (default: '4000 EEW')")
    parser.add_argument("--overwrite", action="store_true", help="Replace non-empty Hint fields too")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)
    data_dir = Path(args.data_dir)

    hint_maps = build_hint_maps(data_dir)
    if not hint_maps:
        print(f"[ERROR] No book*/data.json found under {data_dir}")
        sys.exit(1)

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        work_dir = Path(tmp)
        with zipfile.ZipFile(input_path, "r") as z:
            z.extractall(work_dir)

        kind, sqlite_path = load_collection(work_dir)
        print(f"Using collection format: {kind}")

        apply_hints(sqlite_path, kind, hint_maps, args.notetype, args.overwrite)
        repackage(work_dir, kind, sqlite_path, output_path)


if __name__ == "__main__":
    main()
