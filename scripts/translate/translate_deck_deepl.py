#!/usr/bin/env python3
"""
translate_deck_deepl.py
------------------------
Load an Anki .apkg file, translate the Meaning and Example fields to
Persian using the DeepL API, and write the results into the FaMeaning
and FaExample fields.

Requirements:
    pip install deepl --break-system-packages

Usage (run from the repo root):
    python scripts/translate/translate_deck_deepl.py \
        --input "dist/shared-deck/4000 Essential English Words (all books).apkg" \
        --output "dist/shared-deck/4000 Essential English Words (all books).fa.apkg" \
        --api-key "YOUR_DEEPL_API_KEY" \
        --overwrite            # (optional) re-translate even if a
                                # translation already exists
        --limit 20              # (optional) only process the first
                                # N cards (useful for testing)

Notes:
- The script automatically detects the note type(s) of the cards and
  looks up the field indices by name (Meaning, Example, FaMeaning,
  FaExample).
- If the deck has multiple note types, the script processes every note
  type that contains all four required fields.
- By default, if FaMeaning/FaExample already has content, it will not
  be re-translated (to save time/cost). Use --overwrite to force
  re-translation.
- Media files (images/audio) are copied through untouched.
"""

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile
from pathlib import Path

try:
    import deepl
except ImportError:
    print("The 'deepl' package is not installed. Run: pip install deepl --break-system-packages")
    sys.exit(1)

FIELD_SEP = "\x1f"

# Field names to look for (change these here if your field names differ)
SRC_MEANING = "Meaning"
SRC_EXAMPLE = "Example"
DST_MEANING = "FaMeaning"
DST_EXAMPLE = "FaExample"


def extract_apkg(apkg_path: Path, work_dir: Path) -> Path:
    """Extract the apkg and return the path to the collection database file."""
    with zipfile.ZipFile(apkg_path, "r") as z:
        z.extractall(work_dir)

    # Most exports use collection.anki2; some newer versions use anki21/anki21b
    for name in ("collection.anki21", "collection.anki2", "collection.anki21b"):
        candidate = work_dir / name
        if candidate.exists():
            return candidate

    raise FileNotFoundError("Could not find collection.anki2/anki21 inside the apkg.")


def get_models(conn: sqlite3.Connection) -> dict:
    """
    Return the note type models from the col table.
    Output: { model_id(str): {"name": ..., "flds": [field_name, ...]} }
    """
    cur = conn.cursor()
    cur.execute("SELECT models FROM col")
    row = cur.fetchone()
    models_json = json.loads(row[0])

    result = {}
    for mid, model in models_json.items():
        field_names = [f["name"] for f in model["flds"]]
        result[mid] = {"name": model.get("name", ""), "flds": field_names}
    return result


def translate_deck(
    apkg_path: str,
    output_path: str,
    api_key: str,
    overwrite: bool = False,
    limit: int | None = None,
    source_lang: str = "EN",
    target_lang: str = "FA",
    sleep_between: float = 0.0,
):
    apkg_path = Path(apkg_path)
    output_path = Path(output_path)

    translator = deepl.Translator(api_key)

    with tempfile.TemporaryDirectory() as tmp:
        work_dir = Path(tmp)
        db_path = extract_apkg(apkg_path, work_dir)

        conn = sqlite3.connect(db_path)
        cur = conn.cursor()

        models = get_models(conn)

        # For every model that has both Meaning/Example and
        # FaMeaning/FaExample, compute the field indices
        model_field_idx = {}
        for mid, info in models.items():
            flds = info["flds"]
            needed = [SRC_MEANING, SRC_EXAMPLE, DST_MEANING, DST_EXAMPLE]
            if all(f in flds for f in needed):
                model_field_idx[mid] = {f: flds.index(f) for f in needed}

        if not model_field_idx:
            print("No note type found with Meaning/Example/FaMeaning/FaExample fields.")
            print("Fields found in existing models:")
            for info in models.values():
                print(f"  - {info['name']}: {info['flds']}")
            conn.close()
            return

        cur.execute("SELECT id, mid, flds FROM notes")
        notes = cur.fetchall()

        translated_count = 0
        skipped_count = 0
        processed = 0

        for note_id, mid, flds_str in notes:
            mid_str = str(mid)
            if mid_str not in model_field_idx:
                continue

            if limit is not None and processed >= limit:
                break

            idx = model_field_idx[mid_str]
            fields = flds_str.split(FIELD_SEP)

            meaning = fields[idx[SRC_MEANING]].strip()
            example = fields[idx[SRC_EXAMPLE]].strip()
            fa_meaning = fields[idx[DST_MEANING]].strip()
            fa_example = fields[idx[DST_EXAMPLE]].strip()

            changed = False

            if meaning and (overwrite or not fa_meaning):
                translated = translator.translate_text(
                    meaning, source_lang=source_lang, target_lang=target_lang
                ).text
                fields[idx[DST_MEANING]] = translated
                changed = True
                if sleep_between:
                    time.sleep(sleep_between)

            if example and (overwrite or not fa_example):
                translated = translator.translate_text(
                    example, source_lang=source_lang, target_lang=target_lang
                ).text
                fields[idx[DST_EXAMPLE]] = translated
                changed = True
                if sleep_between:
                    time.sleep(sleep_between)

            if changed:
                new_flds = FIELD_SEP.join(fields)
                cur.execute("UPDATE notes SET flds = ? WHERE id = ?", (new_flds, note_id))
                translated_count += 1
            else:
                skipped_count += 1

            processed += 1
            if processed % 25 == 0:
                print(f"  ... {processed} cards processed")

        conn.commit()
        conn.close()

        print(f"Done: {translated_count} cards translated, {skipped_count} cards skipped (already filled).")

        # Repackage the work_dir back into an apkg (zip) file
        if output_path.exists():
            output_path.unlink()

        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for item in work_dir.rglob("*"):
                if item.is_file():
                    zf.write(item, item.relative_to(work_dir))

        print(f"Output file saved: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Translate Anki deck fields to Persian using DeepL")
    parser.add_argument("--input", required=True, help="Path to the input apkg file")
    parser.add_argument("--output", required=True, help="Path to the output apkg file")
    parser.add_argument("--api-key", required=True, help="DeepL API key")
    parser.add_argument("--overwrite", action="store_true", help="Re-translate even if a translation already exists")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N cards (for testing)")
    parser.add_argument("--sleep", type=float, default=0.0, help="Delay between requests, in seconds")
    args = parser.parse_args()

    translate_deck(
        apkg_path=args.input,
        output_path=args.output,
        api_key=args.api_key,
        overwrite=args.overwrite,
        limit=args.limit,
        sleep_between=args.sleep,
    )


if __name__ == "__main__":
    main()
