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

Each request tries a direct connection first, then --proxy (default
http://127.0.0.1:10808, the same local proxy the Downloder scripts use)
if direct fails, alternating back and forth on further retries -- whichever
one is actually up varies over time, so retrying is more resilient than
committing to just one. Pass --no-proxy to skip the proxy entirely and
only ever try direct. The wait between retries grows following the
Fibonacci sequence (1, 2, 3, 5, 8, ... seconds).

Like add_hints_to_shared_deck.py, this edits the collection in place:
note ids/GUIDs are untouched (only FaMeaning/FaExample + mod/usn change),
so importing the output into a profile that already studies this deck
updates the translation without resetting review history. It also
transparently handles apkg files that use the newer zstd-compressed
collection.anki21b format instead of the legacy collection.anki2.

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
    zai-org/GLM-5.2-FP8
- All currently available models are reasoning models that prepend a
  <think>...</think> block to their answer. clean_translation() strips
  it, and max_tokens is set generously (600) so the model has room to
  finish thinking AND emit the actual translation -- a low max_tokens
  truncates the response mid-<think>, before any Persian text appears.
- Skips notes that already have a Persian translation unless
  --overwrite is passed.
- Leaves all media (images/audio) untouched.
"""

import argparse
import json
import os
import re
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

try:
    import zstandard
except ImportError:
    print("The 'zstandard' package is not installed. Run: pip install zstandard --break-system-packages")
    sys.exit(1)

FIELD_SEP = "\x1f"

# Field names to look for (change these here if your field names differ)
SRC_MEANING = "Meaning"
SRC_EXAMPLE = "Example"
DST_MEANING = "FaMeaning"
DST_EXAMPLE = "FaExample"

BASE_URL = "https://inference.dahl.global/v1"
DEFAULT_MODEL = "MiniMaxAI/MiniMax-M2.7"
DEFAULT_PROXY = "http://127.0.0.1:10808"

SYSTEM_PROMPT = (
    "You are a professional English-to-Persian translator working on "
    "an English vocabulary flashcard deck. Translate the given text "
    "into natural, fluent Persian (Farsi). Output ONLY the Persian "
    "translation. Do not add quotation marks, notes, alternatives, "
    "English text, or any other commentary."
)


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


def load_collection(work_dir: Path):
    """
    Extracts the apkg's collection database and returns (kind, sqlite_path).

    Newer Anki exports keep an empty legacy collection.anki2 stub alongside
    the real, zstd-compressed collection.anki21b -- picking "whichever file
    exists first" (as this script used to) silently opens the empty stub.
    Instead, decompress anki21b if present and pick whichever candidate
    actually holds notes.
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


def get_models(conn: sqlite3.Connection, kind: str) -> dict:
    """
    Return the note type models, regardless of schema kind.
    Output: { model_id(str): {"name": ..., "flds": [field_name, ...]} }
    """
    if kind == "anki21b":
        result = {}
        for ntid, name in conn.execute("SELECT id, name FROM notetypes"):
            rows = conn.execute("SELECT ord, name FROM fields WHERE ntid=? ORDER BY ord", (ntid,)).fetchall()
            result[str(ntid)] = {"name": name, "flds": [n for _ord, n in rows]}
        return result

    row = conn.execute("SELECT models FROM col").fetchone()
    models_json = json.loads(row[0])

    result = {}
    for mid, model in models_json.items():
        field_names = [f["name"] for f in model["flds"]]
        result[mid] = {"name": model.get("name", ""), "flds": field_names}
    return result


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
            if item.name == "_collection21b_decoded.sqlite":
                continue
            if item.suffix in (".wal", ".shm", "-journal"):
                continue
            zf.write(item, item.relative_to(work_dir))

    print(f"Output file saved: {output_path}")


def make_session(proxy: str | None) -> requests.Session:
    session = requests.Session()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


def fib_delay(attempt: int) -> float:
    """Delay before the (attempt+1)-th try: 1, 2, 3, 5, 8, 13, ..."""
    a, b = 1, 2
    for _ in range(attempt - 1):
        a, b = b, a + b
    return float(a)


def pick_session(attempt: int, session_direct: requests.Session, session_proxied: requests.Session | None):
    """
    Alternate connection modes across attempts: try direct first, then the
    proxy, then back to direct, and so on -- either one might be the one
    that's actually working at a given moment. Falls back to direct-only
    if no proxy session was configured (--no-proxy).
    """
    if session_proxied is None:
        return session_direct, "direct"
    if attempt % 2 == 1:
        return session_direct, "direct"
    return session_proxied, "proxy"


def check_model_available(session_direct: requests.Session, session_proxied: requests.Session | None, model_id: str) -> None:
    """
    Query GET /v1/models (public, no auth) and warn if the requested
    model id is not currently listed. Does not raise -- Dahl's own
    docs note that ids can rotate, so this check is advisory only.
    Tries direct first, then the proxy, since either may be the one
    actually reaching the internet right now.
    """
    last_exc = None
    for session, mode in [(session_direct, "direct"), (session_proxied, "proxy")]:
        if session is None:
            continue
        try:
            resp = session.get(f"{BASE_URL}/models", timeout=15)
            resp.raise_for_status()
            data = resp.json()
            available_ids = [m.get("id") for m in data.get("data", [])]
            break
        except Exception as exc:
            last_exc = exc
    else:
        print(f"Warning: could not verify model availability ({last_exc}). Continuing anyway.")
        return

    if available_ids and model_id not in available_ids:
        print(f"Warning: model '{model_id}' was not found in the current GET /v1/models response.")
        print(f"Currently listed models: {available_ids}")
        print("Continuing anyway -- pass a different --model if requests start failing.")


THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def clean_translation(text: str) -> str:
    """Strip common wrapping artifacts a chat model may add despite instructions."""
    # All currently available Dahl models are reasoning models: they
    # prepend a <think>...</think> block with their reasoning before the
    # actual answer. Only the text after it is the translation.
    text = THINK_BLOCK_RE.sub("", text).strip()

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


def call_dahl_chat(
    session_direct: requests.Session,
    session_proxied: requests.Session | None,
    api_key: str,
    model: str,
    text: str,
    max_retries: int = 6,
) -> tuple[str, dict]:
    """
    Send a single chat completion request to Dahl asking for a Persian
    translation of `text`, and return (cleaned_translation, usage) where
    usage is the API's token-count dict ({"prompt_tokens", "completion_tokens",
    "total_tokens"}, empty if the API didn't report it).

    Each retry alternates between a direct connection and the proxy
    (pick_session) -- whichever is actually up varies over time, so
    sticking to one exclusively can fail for a long stretch while the
    other would have worked. The wait between attempts grows following
    the Fibonacci sequence (1, 2, 3, 5, 8, ... seconds) rather than
    doubling, per Dahl's guidance for handling network/node overload.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # Force a fresh connection per request. A pooled/kept-alive
        # connection through a flaky local proxy can go half-dead (proxy
        # drops it without a TCP FIN/RST); requests then blocks waiting on
        # a socket that will never respond, which looks like a hang rather
        # than a clean, retryable error.
        "Connection": "close",
    }
    for attempt in range(1, max_retries + 1):
        session, mode = pick_session(attempt, session_direct, session_proxied)
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            "temperature": 0.2,
            # Generous, and growing on each retry: these are reasoning
            # models that spend a good chunk of the budget on a <think>
            # block before the real answer. temperature=0.2 means a retry
            # with the same max_tokens tends to truncate at the same
            # spot again (observed with "noise": 4/4 identical truncated
            # attempts at 600) -- only a bigger budget actually helps.
            "max_tokens": 600 + (attempt - 1) * 500,
        }
        try:
            # (connect_timeout, read_timeout): fail fast on a stuck/dead
            # connection instead of blocking for a full minute per attempt.
            resp = session.post(
                f"{BASE_URL}/chat/completions", headers=headers, json=payload, timeout=(10, 45)
            )
        except requests.exceptions.RequestException as exc:
            if attempt == max_retries:
                raise RuntimeError(f"Network error calling Dahl API: {exc}") from exc
            delay = fib_delay(attempt)
            next_session, next_mode = pick_session(attempt + 1, session_direct, session_proxied)
            print(f"    [retry {attempt}/{max_retries}] {mode} network error ({exc}); "
                  f"retrying via {next_mode} in {delay:.0f}s...")
            time.sleep(delay)
            continue

        if resp.status_code == 200:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage") or {}

            # A response cut off mid-<think> (no closing tag) never
            # reached the actual translation; retry rather than saving
            # a blank/garbage field.
            truncated_mid_think = "<think>" in content.lower() and "</think>" not in content.lower()
            cleaned = clean_translation(content)

            if not truncated_mid_think and cleaned:
                return cleaned, usage

            if attempt == max_retries:
                raise RuntimeError(
                    f"Dahl API kept returning an empty/truncated translation after {max_retries} "
                    f"attempts (raw content: {content!r})"
                )
            delay = fib_delay(attempt)
            print(f"    [retry {attempt}/{max_retries}] empty/truncated response; retrying in {delay:.0f}s...")
            time.sleep(delay)
            continue

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
            delay = fib_delay(attempt)
            print(f"    [retry {attempt}/{max_retries}] HTTP {resp.status_code}; retrying in {delay:.0f}s...")
            time.sleep(delay)
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
    proxy: str | None = DEFAULT_PROXY,
):
    apkg_path = Path(apkg_path)
    output_path = Path(output_path)
    session_direct = make_session(None)
    session_proxied = make_session(proxy) if proxy else None

    check_model_available(session_direct, session_proxied, model)

    # Resume from a previous partial run if --output already exists: it
    # already has this run's translations saved (translate_deck commits
    # and repackages periodically/on failure), so re-extracting from
    # --input every time would silently re-translate (and re-bill) every
    # note done so far. --input is only the starting point for the very
    # first run.
    source_path = output_path if output_path.exists() else apkg_path
    if source_path == output_path:
        print(f"Resuming from existing output: {output_path}")

    with tempfile.TemporaryDirectory() as tmp:
        work_dir = Path(tmp)
        with zipfile.ZipFile(source_path, "r") as z:
            z.extractall(work_dir)

        kind, db_path = load_collection(work_dir)
        print(f"Using collection format: {kind}")

        conn = connect(db_path)
        translated_count = 0
        skipped_count = 0
        has_matching_notetype = False
        error = None
        tokens = {"prompt": 0, "completion": 0, "total": 0}

        try:
            cur = conn.cursor()
            models = get_models(conn, kind)

            # For every model that has both Meaning/Example and
            # FaMeaning/FaExample, compute the field indices
            model_field_idx = {}
            for mid, info in models.items():
                flds = info["flds"]
                needed = [SRC_MEANING, SRC_EXAMPLE, DST_MEANING, DST_EXAMPLE]
                if all(f in flds for f in needed):
                    field_idx = {f: flds.index(f) for f in needed}
                    field_idx["Word"] = flds.index("Word") if "Word" in flds else None
                    model_field_idx[mid] = field_idx

            if not model_field_idx:
                print("No note type found with Meaning/Example/FaMeaning/FaExample fields.")
                print("Fields found in existing models:")
                for info in models.values():
                    print(f"  - {info['name']}: {info['flds']}")
                return

            has_matching_notetype = True
            cur.execute("SELECT id, mid, flds FROM notes")
            notes = cur.fetchall()

            total_candidates = sum(1 for _, mid, _ in notes if str(mid) in model_field_idx)
            total_to_process = min(limit, total_candidates) if limit is not None else total_candidates
            print(f"{total_candidates} notes need translating; processing {total_to_process}.")

            def add_usage(usage: dict) -> int:
                p, c, t = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), usage.get("total_tokens", 0)
                tokens["prompt"] += p
                tokens["completion"] += c
                tokens["total"] += t
                return t

            processed = 0

            for note_id, mid, flds_str in notes:
                mid_str = str(mid)
                if mid_str not in model_field_idx:
                    continue

                if limit is not None and processed >= limit:
                    break

                idx = model_field_idx[mid_str]
                fields = flds_str.split(FIELD_SEP)
                word = fields[idx["Word"]].strip() if idx["Word"] is not None else f"note {note_id}"

                meaning = fields[idx[SRC_MEANING]].strip()
                example = fields[idx[SRC_EXAMPLE]].strip()
                fa_meaning = fields[idx[DST_MEANING]].strip()
                fa_example = fields[idx[DST_EXAMPLE]].strip()

                changed = False
                note_tokens = 0
                progress = f"[{processed + 1}/{total_to_process}] ({(processed + 1) / total_to_process:.1%}) {word}"

                if meaning and (overwrite or not fa_meaning):
                    translated, usage = call_dahl_chat(session_direct, session_proxied, api_key, model, meaning)
                    fields[idx[DST_MEANING]] = translated
                    changed = True
                    note_tokens += add_usage(usage)
                    print(f"{progress} | FaMeaning: {translated}")
                    if sleep_between:
                        time.sleep(sleep_between)

                if example and (overwrite or not fa_example):
                    translated, usage = call_dahl_chat(session_direct, session_proxied, api_key, model, example)
                    fields[idx[DST_EXAMPLE]] = translated
                    changed = True
                    note_tokens += add_usage(usage)
                    print(f"{progress} | FaExample: {translated}")
                    if sleep_between:
                        time.sleep(sleep_between)

                if changed:
                    new_flds = FIELD_SEP.join(fields)
                    # Anki's importer matches notes by GUID but only overwrites
                    # an existing note's fields if the incoming `mod` is newer
                    # than the local note's `mod`; leaving `mod` untouched would
                    # make this update a silent no-op on a collection that
                    # already has these notes. usn=-1 is Anki's own "modified
                    # locally, needs sync" marker for edited notes.
                    cur.execute(
                        "UPDATE notes SET flds=?, mod=?, usn=-1 WHERE id=?",
                        (new_flds, int(time.time()), note_id),
                    )
                    translated_count += 1
                    print(f"{progress} | tokens: +{note_tokens} (cumulative: {tokens['total']})")
                else:
                    skipped_count += 1

                processed += 1
                if processed % 25 == 0:
                    # Commit periodically so a failure later in the run (bad
                    # key, exhausted quota, network drop) doesn't roll back
                    # translations already paid for/done in this run.
                    conn.commit()
                    print(
                        f"  ... {processed}/{total_to_process} processed (progress saved) | "
                        f"tokens so far: prompt={tokens['prompt']} completion={tokens['completion']} total={tokens['total']}"
                    )
        except Exception as exc:
            error = exc
        finally:
            conn.commit()
            conn.close()

        print(f"Done: {translated_count} cards translated, {skipped_count} cards skipped (already filled).")
        print(f"Total tokens used: prompt={tokens['prompt']} completion={tokens['completion']} total={tokens['total']}")

        if has_matching_notetype:
            # Repackage even if the loop above raised partway through, so
            # whatever was translated before the failure isn't lost.
            repackage(work_dir, kind, db_path, output_path)

        if error is not None:
            raise error


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
    parser.add_argument("--proxy", default=DEFAULT_PROXY, help=f"HTTP(S) proxy for Dahl requests (default: {DEFAULT_PROXY})")
    parser.add_argument("--no-proxy", action="store_true", help="Disable the proxy and connect directly")
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
        proxy=None if args.no_proxy else args.proxy,
    )


if __name__ == "__main__":
    main()
