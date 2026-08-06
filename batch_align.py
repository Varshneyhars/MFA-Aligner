"""Bulk forced-alignment client for the MFA Cloud Run service.

Sized for tens of thousands of short utterances. The throughput levers are:

  * batch size   - every item in a request is aligned in ONE mfa invocation, so
                   the acoustic model load and database setup are amortised
                   across the whole batch. Bigger batches are strictly better
                   until they risk the request timeout.
  * concurrency  - one alignment per instance (MFA saturates all cores), so
                   client concurrency should equal the service's max-instances.

Input is a manifest: one JSON object per line with an audio source and its
transcript.

    {"audio_url": "gs://bucket/a.wav", "transcript": "the words spoken"}
    {"gcs_uri": "gs://bucket/b.wav", "transcript": "more words"}

Run:

    python batch_align.py --service-url https://mfa-aligner-xxxx.run.app \
        --input manifest.jsonl --output aligned.jsonl --concurrency 20

Re-running skips anything already in --output, so an interrupted job resumes
rather than re-paying for completed work.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, Iterator, List, Optional

import requests

# The server caps this via MFA_MAX_BATCH_ITEMS. Larger batches amortise MFA's
# fixed per-invocation cost better, so fill them.
DEFAULT_BATCH_SIZE = 64
# One alignment per instance, so this should track the service's --max-instances.
DEFAULT_CONCURRENCY = 10
AUDIO_KEYS = ("audio", "audio_url", "audio_base64", "url", "gcs_uri")


class TokenCache:
    """gcloud identity tokens last an hour; mint once and reuse."""

    def __init__(self, static_token: Optional[str] = None, ttl_s: int = 2700):
        self._static, self._ttl = static_token, ttl_s
        self._token: Optional[str] = None
        self._minted = 0.0
        self._lock = threading.Lock()

    def get(self) -> str:
        if self._static:
            return self._static
        with self._lock:
            if self._token is None or (time.time() - self._minted) > self._ttl:
                self._token = subprocess.run(
                    ["gcloud", "auth", "print-identity-token"],
                    capture_output=True, text=True, check=True, shell=(os.name == "nt"),
                ).stdout.strip()
                self._minted = time.time()
            return self._token


def source_of(item: Dict[str, Any]) -> str:
    """The audio URI, used as the resume key."""
    for k in AUDIO_KEYS:
        if item.get(k):
            return str(item[k])
    return ""


def read_manifest(path: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"  skipping line {n}: {e}", file=sys.stderr)
                continue
            if not source_of(obj):
                print(f"  skipping line {n}: no audio source", file=sys.stderr)
                continue
            if not (obj.get("transcript") or obj.get("text")):
                print(f"  skipping line {n}: no transcript", file=sys.stderr)
                continue
            items.append(obj)
    return items


def load_done(path: str) -> set:
    done = set()
    if not os.path.exists(path):
        return done
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # torn last line from a hard kill
            if rec.get("source"):
                done.add(rec["source"])
    return done


def chunk(items: List[Dict[str, Any]], size: int) -> Iterator[List[Dict[str, Any]]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def send(session: requests.Session, url: str, tokens: TokenCache,
         batch: List[Dict[str, Any]], response_format: str,
         timeout_s: float, retries: int) -> List[Dict[str, Any]]:
    payload = {"audio": batch, "response_format": response_format}
    last_err = ""
    for attempt in range(retries + 1):
        try:
            resp = session.post(url.rstrip("/") + "/align", json=payload,
                                headers={"Authorization": f"Bearer {tokens.get()}"},
                                timeout=timeout_s)
            if resp.status_code == 200:
                body = resp.json()
                results = body.get("results") or [body]
                return [{**r, "source": source_of(s)} for s, r in zip(batch, results)]
            # 400 means the payload is wrong; retrying re-pays for the same
            # rejection. Only 429/5xx are worth another attempt.
            if resp.status_code < 500 and resp.status_code != 429:
                last_err = f"HTTP {resp.status_code}: {resp.text[:300]}"
                break
            last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
        except requests.RequestException as e:
            last_err = f"{type(e).__name__}: {e}"
        if attempt < retries:
            backoff = min(60, 2 ** attempt * 5)
            print(f"  retry {attempt + 1}/{retries} in {backoff}s ({last_err[:110]})", file=sys.stderr)
            time.sleep(backoff)
    return [{"source": source_of(s), "error": last_err} for s in batch]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--service-url", required=True)
    ap.add_argument("--input", required=True, help="JSONL manifest: {audio_url|gcs_uri, transcript}")
    ap.add_argument("--output", default="aligned.jsonl")
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                    help="Set to the service's --max-instances (1 alignment per instance)")
    ap.add_argument("--response-format", default="json", choices=["json", "words", "phones"])
    ap.add_argument("--timeout", type=float, default=3600.0)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--token", default=None)
    args = ap.parse_args()

    items = read_manifest(args.input)
    done = load_done(args.output)
    todo = [i for i in items if source_of(i) not in done]

    print(f"{len(items):,} manifest entries | {len(done):,} already aligned | {len(todo):,} to do")
    if not todo:
        print("Nothing to do.")
        return 0

    batches = list(chunk(todo, args.batch_size))
    print(f"{len(batches):,} batches of up to {args.batch_size}, {args.concurrency} in flight\n")

    tokens = TokenCache(args.token)
    session = requests.Session()
    lock = threading.Lock()
    started = time.perf_counter()
    ok = failed = 0
    audio_s = 0.0

    with open(args.output, "a", encoding="utf-8") as out, \
            ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(send, session, args.service_url, tokens, b,
                               args.response_format, args.timeout, args.retries)
                   for b in batches]
        for n, fut in enumerate(as_completed(futures), start=1):
            for r in fut.result():
                with lock:
                    out.write(json.dumps(r, ensure_ascii=False) + "\n")
                    if "error" in r:
                        failed += 1
                    else:
                        ok += 1
                        audio_s += (r.get("metrics") or {}).get("audio_seconds", 0.0)
            out.flush()  # a crash must not lose completed work
            el = time.perf_counter() - started
            eta = f" | ETA {el / n * (len(batches) - n) / 60:.0f} min" if n < len(batches) else ""
            print(f"[{n}/{len(batches)}] ok={ok:,} failed={failed:,} "
                  f"| {audio_s / 3600:.2f} h audio | {el / 60:.1f} min{eta}")

    el = time.perf_counter() - started
    print(f"\nDone in {el / 60:.1f} min — {ok:,} aligned, {failed:,} failed")
    if audio_s:
        print(f"Processed {audio_s / 3600:.2f} h of audio "
              f"({audio_s / el:.1f}x realtime aggregate) -> {args.output}")
    if failed:
        print(f"Re-run the same command to retry the {failed:,} failures.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
