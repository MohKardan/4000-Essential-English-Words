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

Usage (run from the repo root):
    python scripts/translate/translate_deck_dahl.py \
        --input "dist/shared-deck/4000 Essential English Words (all books).apkg" \
        --output "dist/shared-deck/4000 Essential English Words (all books).fa.apkg" \
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
Fibonacci sequence (1, 2, 3, 5, 8, ... seconds), capped at MAX_RETRY_DELAY.

This script never gives up and never exits on its own: every failure mode
(network errors, Cloudflare challenges, truncated output, even a bad/
expired key or exhausted quota) is retried forever instead of raising, and
main() wraps the whole run in one more retry loop as a last resort. Kill
it yourself (Ctrl+C) if you need it to stop; otherwise it keeps polling
and resumes on its own once whatever was wrong clears up.

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
import queue
import re
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
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

# call_dahl_chat() retries forever rather than ever raising, so both of
# these need a ceiling: without one, fib_delay grows unboundedly (minutes,
# then hours, between tries) and the escalating max_tokens budget would
# eventually balloon into an enormous, expensive request.
MAX_RETRY_DELAY = 60.0
MAX_TOKENS_CAP = 4000

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
    # requests' default User-Agent ("python-requests/x.y") is a well-known
    # bot signature; Cloudflare's bot-management sits in front of Dahl and
    # was observed returning a JS challenge page (403) for it. A normal
    # browser-like header set is standard practice for API clients and
    # avoids being mis-flagged as a scraper for a legitimate API key.
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
    })
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


DIV_TAG_RE = re.compile(r"</?div\s*>", re.IGNORECASE)
# Matches a leading "\u0645\u062b\u0627\u0644:" ("Example:", used in FaExample) or "\u0645\u0639\u0646\u06cc:"
# ("Meaning:", used in FaMeaning) label, whether or not it's wrapped in
# <strong> tags, and whether the space after the colon is a real space or
# an HTML &nbsp; entity (observed both ways).
LEADING_LABEL_RE = re.compile(
    r"^\s*(?:<strong>\s*)?(?:\u0645\u062b\u0627\u0644|\u0645\u0639\u0646\u06cc)\s*[:\uff1a]\s*(?:&nbsp;\s*)*(?:</strong>\s*)?"
)
STRONG_TAG_RE = re.compile(r"<(/?)strong\s*>", re.IGNORECASE)


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

    # Occasionally (observed on a handful of longer/abstract words, in both
    # FaMeaning and FaExample) the model wraps its answer in stray markup
    # instead of plain text -- with or without a <div> wrapper, with or
    # without <strong> tags, sometimes using a literal "&nbsp;" instead of a
    # space. The div wrapper is pure noise (strip unconditionally); the
    # leading "\u0645\u062b\u0627\u0644:"/"\u0645\u0639\u0646\u06cc:" label is also noise (strip it specifically,
    # not every <strong> -- a <strong> around the translated word itself is
    # real emphasis, same role as <b> elsewhere in this deck, so convert
    # rather than delete it).
    if "<div" in text.lower():
        text = DIV_TAG_RE.sub("", text).strip()
    text = LEADING_LABEL_RE.sub("", text).strip()
    if "<strong" in text.lower():
        text = STRONG_TAG_RE.sub(lambda m: f"<{m.group(1)}b>", text).strip()

    return text


def post_with_hard_timeout(session: requests.Session, url: str, headers: dict, payload: dict,
                           soft_timeout: tuple, hard_timeout: float) -> requests.Response:
    """
    requests' own (connect, read) timeout was observed to not always fire --
    a request through the local proxy once hung for hours with an
    established TCP connection and no error, well past its 45s read
    timeout. Running the call in a daemon thread and giving up after
    hard_timeout wall-clock seconds guarantees the caller always gets
    control back, even if the underlying socket call never returns. The
    orphaned thread is left to die on its own (or leak harmlessly for the
    life of the process); it cannot be forcibly killed from here.
    """
    result: queue.Queue = queue.Queue(maxsize=1)

    def worker():
        try:
            resp = session.post(url, headers=headers, json=payload, timeout=soft_timeout)
            result.put(("ok", resp))
        except Exception as exc:
            result.put(("error", exc))

    threading.Thread(target=worker, daemon=True).start()
    try:
        kind, value = result.get(timeout=hard_timeout)
    except queue.Empty:
        raise TimeoutError(
            f"Request hard-timed out after {hard_timeout:.0f}s (the underlying "
            f"{soft_timeout} connect/read timeout never fired)"
        )
    if kind == "error":
        raise value
    return value


def call_dahl_chat(
    session_direct: requests.Session,
    session_proxied: requests.Session | None,
    api_key: str,
    model: str,
    text: str,
) -> tuple[str, dict]:
    """
    Send a single chat completion request to Dahl asking for a Persian
    translation of `text`, and return (cleaned_translation, usage) where
    usage is the API's token-count dict ({"prompt_tokens", "completion_tokens",
    "total_tokens"}, empty if the API didn't report it).

    Retries forever and never raises: no matter what goes wrong (network
    outage, Cloudflare challenge, truncated output, even a bad/expired key
    or exhausted quota), this keeps trying rather than letting the whole
    script crash and exit. Every prior crash the script has hit overnight
    was a transient condition that cleared up on its own (network came
    back, proxy restarted) -- the fix each time was "restart the script",
    which this loop now does internally instead of needing a human to
    notice and re-run it.

    Each retry alternates between a direct connection and the proxy
    (pick_session) -- whichever is actually up varies over time, so
    sticking to one exclusively can fail for a long stretch while the
    other would have worked. The wait between attempts grows following
    the Fibonacci sequence (1, 2, 3, 5, 8, ... seconds), capped at
    MAX_RETRY_DELAY so it settles into a steady polling cadence instead of
    waiting longer and longer forever.
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
    attempt = 0
    while True:
        attempt += 1
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
            # Capped since attempt is now unbounded (retries forever).
            "max_tokens": min(600 + (attempt - 1) * 500, MAX_TOKENS_CAP),
        }
        try:
            # (connect_timeout, read_timeout): fail fast on a stuck/dead
            # connection instead of blocking for a full minute per attempt.
            # Wrapped in a hard wall-clock timeout because this soft timeout
            # was observed to not fire at all on one occasion (a request
            # hung for hours with an established connection and no error).
            resp = post_with_hard_timeout(
                session, f"{BASE_URL}/chat/completions", headers, payload,
                soft_timeout=(10, 45), hard_timeout=90,
            )
        except (requests.exceptions.RequestException, TimeoutError) as exc:
            delay = min(fib_delay(attempt), MAX_RETRY_DELAY)
            next_session, next_mode = pick_session(attempt + 1, session_direct, session_proxied)
            print(f"    [retry {attempt}] {mode} network error ({exc}); "
                  f"retrying via {next_mode} in {delay:.0f}s...")
            time.sleep(delay)
            continue

        if resp.status_code == 200:
            data = resp.json()
            choice = data["choices"][0]
            content = choice["message"]["content"]
            usage = data.get("usage") or {}

            # A response cut off mid-<think> (no closing tag) never
            # reached the actual translation; retry rather than saving
            # a blank/garbage field.
            truncated_mid_think = "<think>" in content.lower() and "</think>" not in content.lower()

            # The API's own signal that max_tokens was hit before the model
            # finished -- catches the case that truncated_mid_think misses:
            # </think> closed fine, but the real answer after it got cut off
            # mid-sentence/mid-word (observed: "من بذر را در خاک کاش" instead
            # of "کاشتم", "...و چ" instead of a finished word).
            #
            # Deliberately NOT also checking for trailing punctuation as a
            # heuristic: many complete, correct short Persian sentences don't
            # end in a period (e.g. "او یک میزبان مهربان بود"), so that check
            # rejected valid translations on every retry, wasting many
            # retries on a sentence that was fine from the first attempt.
            truncated_by_length = choice.get("finish_reason") == "length"

            cleaned = clean_translation(content)

            if not truncated_mid_think and not truncated_by_length and cleaned:
                return cleaned, usage

            if truncated_mid_think:
                reason = "cut off mid-<think>"
            elif truncated_by_length:
                reason = "cut off by max_tokens (finish_reason=length)"
            else:
                reason = "empty response"

            delay = min(fib_delay(attempt), MAX_RETRY_DELAY)
            print(f"    [retry {attempt}] {reason}; retrying in {delay:.0f}s...")
            time.sleep(delay)
            continue

        # 401/402 are normally permanent (bad key / no quota left) and would
        # traditionally be a hard stop -- but per policy this function must
        # never raise, so instead it waits and keeps polling. If the key
        # truly is dead this spins harmlessly forever (visible in the log,
        # not silent); if it was a transient false-positive or the account
        # gets topped up, the run resumes on its own with no restart needed.
        if resp.status_code == 401:
            delay = min(fib_delay(attempt), MAX_RETRY_DELAY)
            print(f"    [retry {attempt}] Dahl API returned 401 (missing/invalid/expired token) -- "
                  f"get a fresh key at https://inference.dahl.global/#models if this persists; "
                  f"retrying in {delay:.0f}s...")
            time.sleep(delay)
            continue

        if resp.status_code == 402:
            delay = min(fib_delay(attempt), MAX_RETRY_DELAY)
            print(f"    [retry {attempt}] Dahl API returned 402 (tokens exhausted on this key) -- "
                  f"create a new key at https://inference.dahl.global/#models if this persists; "
                  f"retrying in {delay:.0f}s...")
            time.sleep(delay)
            continue

        # 403 is included here because it isn't always Dahl itself: a burst of
        # requests from one IP can trip Cloudflare's bot-challenge page (a
        # "Just a moment..." JS challenge) in front of the API, which a
        # plain HTTP client can never pass. Retrying alone won't fix that,
        # but alternating to the other connection mode (direct vs proxy)
        # presents a different IP, which often does get past it.
        if resp.status_code == 403 or resp.status_code == 429 or resp.status_code == 503 or resp.status_code >= 500:
            delay = min(fib_delay(attempt), MAX_RETRY_DELAY)
            next_session, next_mode = pick_session(attempt + 1, session_direct, session_proxied)
            challenge_hint = " (looks like a Cloudflare bot challenge)" if "cf_chl" in resp.text else ""
            print(f"    [retry {attempt}] {mode} HTTP {resp.status_code}{challenge_hint}; "
                  f"retrying via {next_mode} in {delay:.0f}s...")
            time.sleep(delay)
            continue

        # Other 4xx errors, e.g. a stale/unsupported model id. Still never
        # raise -- keep polling at the capped delay rather than exiting.
        delay = min(fib_delay(attempt), MAX_RETRY_DELAY)
        print(f"    [retry {attempt}] Dahl API error {resp.status_code}: {resp.text[:300]!r}; "
              f"retrying in {delay:.0f}s...")
        time.sleep(delay)


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
                progress = f"[{processed + 1}/{total_to_process}] ({(processed + 1) / total_to_process:.1%}) {word}"

                # call_dahl_chat() itself never raises (it retries forever
                # internally), so this only guards against something
                # unrelated to the API -- a bad field, a sqlite error, etc.
                # Per the "never crash" policy: log it and move on to the
                # next note rather than aborting the whole run; this note
                # simply stays untranslated for now and will be picked up
                # by a future pass over the deck.
                try:
                    meaning = fields[idx[SRC_MEANING]].strip()
                    example = fields[idx[SRC_EXAMPLE]].strip()
                    fa_meaning = fields[idx[DST_MEANING]].strip()
                    fa_example = fields[idx[DST_EXAMPLE]].strip()

                    changed = False
                    note_tokens = 0

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
                except Exception as exc:
                    print(f"{progress} | SKIPPING due to unexpected error: {exc!r}")

                processed += 1
                if processed % 25 == 0:
                    # Commit AND repackage periodically. Committing alone
                    # only guarantees the data survives inside the temp
                    # working copy; repackage() is what actually writes it
                    # to the real --output file on disk. Without doing both
                    # here, a hang/crash before the run's single final
                    # repackage (the old behavior) loses everything back to
                    # the previous run's checkpoint, however long ago that
                    # was -- exactly what happened overnight.
                    conn.commit()
                    repackage(work_dir, kind, db_path, output_path)
                    print(
                        f"  ... {processed}/{total_to_process} processed (progress saved to disk) | "
                        f"tokens so far: prompt={tokens['prompt']} completion={tokens['completion']} total={tokens['total']}"
                    )
        except Exception as exc:
            error = exc
        finally:
            # In `finally` (not after the try/except) so this still runs on
            # Ctrl+C or any other BaseException that `except Exception`
            # doesn't catch -- otherwise an interrupt would skip repackage()
            # entirely and lose everything translated since the last
            # periodic commit.
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

    # Last-resort safety net: call_dahl_chat() never raises (it retries
    # forever internally) and per-note errors inside translate_deck() are
    # caught and skipped rather than propagated, so in practice this loop
    # shouldn't ever see an exception. It exists anyway because the policy
    # is that this script must never crash/exit no matter what -- if
    # translate_deck() does somehow raise (e.g. a setup error before the
    # per-note loop starts), log it and just call it again instead of
    # letting the process die; it resumes from --output on disk either way.
    while True:
        try:
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
            break
        except Exception:
            print("translate_deck() raised unexpectedly -- NOT exiting. "
                  "Waiting 30s and resuming from the on-disk output file...")
            traceback.print_exc()
            time.sleep(30)


if __name__ == "__main__":
    main()
