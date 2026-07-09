#!/usr/bin/env python3
"""
translate_deck_dahl.py
------------------------
Load an Anki .apkg file, translate the Meaning and Example fields to
Persian using a chat model served through the Dahl Inference API
(an OpenAI-compatible endpoint over open-weight models), and write
the results into the FaMeaning and FaExample fields.

This is a drop-in alternative to translate_deck_deepl.py for cases
where the DeepL API is not reliably reachable (e.g. regional access
restrictions).

Requirements:
    pip install requests --break-system-packages

Usage:
    python translate_deck_dahl.py \
        --input "4000_Essential_Words.apkg" \
        --output "4000_Essential_Words_fa.apkg" \
        --api-key "YOUR_DAHL_API_KEY" \
        --model "MiniMaxAI/MiniMax-M2.7" \
        --overwrite \
        --limit 20 \
        --sleep 0.5

If --api-key is omitted, the script reads it from the DAHL_API_KEY
environment variable. Get a key with no signup at:
    https://inference.dahl.global/#models
or:
    curl -X POST https://inference.dahl.global/tokens

Notes:
- Dahl is an OpenAI-compatible API over open-weight chat models, not a
  dedicated translation service like DeepL. This script prompts the
  model to return only the translated text and strips common wrapping
  artifacts (quotes, "Translation:" prefixes), but occasional noisy
  output is still possible -- spot check a sample before publishing.
- Model ids on Dahl can change over time. Before translating, this
  script queries GET /v1/models and prints a warning (rather than
  guessing) if the requested --model id is not currently listed.
- As of this writing, available model ids include:
    MiniMaxAI/MiniMax-M2.7   (default -- general chat and coding)
    moonshotai/Kimi-K2.6     (long-context / reasoning)
  zai-org/GLM-5.2 is listed on the network but marked "coming soon"
  and is not yet available for inference.
- Skips notes that already have a Persian translation unless
  --overwrite is passed.
- Leaves all media (images/audio) untouched.
"""

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
import zipfile
from pathlib import Path

try:
    import requests
except ImportError:
    print("The 'requests' package is not installed. Run: pip install requests --break-system-packages")
    sys.exit(1)

FIELD_SEP = "\x1f"

# Field names to look for (change these here if your field names differ)
SRC_MEANING = "Meaning"
SRC_EXAMPLE = "Example"
DST_MEANING = "FaMeaning"
DST_EXAMPLE = "FaExample"

BASE_URL = "https://inference.dahl.global/v1"
DEFAULT_MODEL = "MiniMaxAI/MiniMax-M2.7"

SYSTEM_PROMPT = (
    "You are a professional English-to-Persian translator working on "
    "an English vocabulary flashcard deck. Translate the given text "
    "into natural, fluent Persian (Farsi). Output ONLY the Persian "
    "translation. Do not add quotation marks, notes, alternatives, "
    "English text, or any other commentary."
)


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


def check_model_available(model_id: str) -> None:
    """
    Query GET /v1/models (public, no auth) and warn if the requested
    model id is not currently listed. Does not raise -- Dahl's own
    docs note that ids can rotate, so this check is advisory only.
    """
    try:
        resp = requests.get(f"{BASE_URL}/models", timeout=15)
        resp.raise_for_status()
        data = resp.json()
        available_ids = [m.get("id") for m in data.get("data", [])]
    except Exception as exc:
        print(f"Warning: could not verify model availability ({exc}). Continuing anyway.")
        return

    if available_ids and model_id not in available_ids:
        print(f"Warning: model '{model_id}' was not found in the current GET /v1/models response.")
        print(f"Currently listed models: {available_ids}")
        print("Continuing anyway -- pass a different --model if requests start failing.")


def clean_translation(text: str) -> str:
    """Strip common wrapping artifacts a chat model may add despite instructions."""
    text = text.strip()

    # Strip a leading English label like "Translation:" or "Persian:"
    for prefix in ("Translation:", "Persian:", "Farsi:"):
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix):].strip()

    # Strip a single layer of wrapping quotes, if present
    pairs = [('"', '"'), ("'", "'"), ("\u201c", "\u201d")]
    for left, right in pairs:
        if len(text) >= 2 and text.startswith(left) and text.endswith(right):
            text = text[1:-1].strip()
            break

    return text


def call_dahl_chat(api_key: str, model: str, text: str, max_retries: int = 4) -> str:
    """
    Send a single chat completion request to Dahl asking for a Persian
    translation of `text`, and return the cleaned translated string.
    Retries with short exponential backoff on 429/503/5xx, per Dahl's
    own documented guidance for handling network/node overload.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "temperature": 0.2,
        "max_tokens": 200,
    }

    delay = 2.0
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(
                f"{BASE_URL}/chat/completions", headers=headers, json=payload, timeout=60
            )
        except requests.exceptions.RequestException as exc:
            if attempt == max_retries:
                raise RuntimeError(f"Network error calling Dahl API: {exc}") from exc
            time.sleep(delay)
            delay *= 2
            continue

        if resp.status_code == 200:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            return clean_translation(content)

        if resp.status_code == 401:
            raise RuntimeError(
                "Dahl API returned 401 (missing/invalid/expired token). "
                "Get a fresh key at https://inference.dahl.global/#models"
            )

        if resp.status_code == 402:
            raise RuntimeError(
                "Dahl API returned 402 (available tokens exhausted on this key). "
                "Create a new key at https://inference.dahl.global/#models"
            )

        if resp.status_code == 429 or resp.status_code == 503 or resp.status_code >= 500:
            if attempt == max_retries:
                raise RuntimeError(f"Dahl API error {resp.status_code} after {max_retries} retries: {resp.text}")
            time.sleep(delay)
            delay *= 2
            continue

        # Other 4xx errors, e.g. a stale/unsupported model id
        raise RuntimeError(f"Dahl API error {resp.status_code}: {resp.text}")

    raise RuntimeError("Exhausted retries calling Dahl API.")


def translate_deck(
    apkg_path: str,
    output_path: str,
    api_key: str,
    model: str = DEFAULT_MODEL,
    overwrite: bool = False,
    limit: int | None = None,
    sleep_between: float = 0.0,
):
    apkg_path = Path(apkg_path)
    output_path = Path(output_path)

    check_model_available(model)

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
                fields[idx[DST_MEANING]] = call_dahl_chat(api_key, model, meaning)
                changed = True
                if sleep_between:
                    time.sleep(sleep_between)

            if example and (overwrite or not fa_example):
                fields[idx[DST_EXAMPLE]] = call_dahl_chat(api_key, model, example)
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
    parser = argparse.ArgumentParser(description="Translate Anki deck fields to Persian using the Dahl Inference API")
    parser.add_argument("--input", required=True, help="Path to the input apkg file")
    parser.add_argument("--output", required=True, help="Path to the output apkg file")
    parser.add_argument(
        "--api-key",
        default=os.environ.get("DAHL_API_KEY"),
        help="Dahl API key (Bearer token). Defaults to the DAHL_API_KEY environment variable.",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Dahl model id to use (default: {DEFAULT_MODEL})")
    parser.add_argument("--overwrite", action="store_true", help="Re-translate even if a translation already exists")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N cards (for testing)")
    parser.add_argument("--sleep", type=float, default=0.0, help="Delay between requests, in seconds")
    args = parser.parse_args()

    if not args.api_key:
        print("No API key provided. Pass --api-key or set the DAHL_API_KEY environment variable.")
        print("Get one with no signup at: https://inference.dahl.global/#models")
        sys.exit(1)

    translate_deck(
        apkg_path=args.input,
        output_path=args.output,
        api_key=args.api_key,
        model=args.model,
        overwrite=args.overwrite,
        limit=args.limit,
        sleep_between=args.sleep,
    )


if __name__ == "__main__":
    main()
