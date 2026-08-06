"""Montreal Forced Aligner core — forced alignment of audio against a transcript.

Platform-neutral: no HTTP framework, no cloud SDK at import time.

MFA is not an ASR system. It takes audio you already have a transcript for and
works out *when* each word and phone was spoken. Kaldi does the work, on CPU —
there is no GPU path worth using here, so everything below is tuned around
parallelising across cores instead.

The unit of work is a corpus directory, not a file. MFA fans out across
--num_jobs workers over everything in that directory, so aligning 32 clips in
one invocation is dramatically cheaper than 32 invocations: the acoustic model
loads once and every core stays busy.
"""
import base64
import binascii
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

ACOUSTIC_MODEL = os.environ.get("MFA_ACOUSTIC_MODEL", "english_mfa")
DICTIONARY = os.environ.get("MFA_DICTIONARY", "english_mfa")
SAMPLE_RATE = 16000  # MFA's pretrained English models expect 16 kHz mono.

# One MFA job per core is the documented sweet spot. Cloud Run reports the
# instance's full vCPU count here, so this scales with whatever we deploy.
NUM_JOBS = int(os.environ.get("MFA_NUM_JOBS", str(os.cpu_count() or 4)))
ALIGN_TIMEOUT_S = float(os.environ.get("MFA_ALIGN_TIMEOUT_S", "3000"))
# Parallel downloads during corpus staging. Network-bound, so this can exceed
# the core count — the CPUs are idle during this phase anyway.
FETCH_WORKERS = int(os.environ.get("MFA_FETCH_WORKERS", "16"))
BEAM = int(os.environ.get("MFA_BEAM", "10"))
RETRY_BEAM = int(os.environ.get("MFA_RETRY_BEAM", "40"))
SINGLE_SPEAKER = os.environ.get("MFA_SINGLE_SPEAKER", "true").lower() == "true"

MAX_AUDIO_DURATION_S = float(os.environ.get("MFA_MAX_AUDIO_DURATION_S", "3600"))
MAX_DOWNLOAD_BYTES = int(os.environ.get("MFA_MAX_DOWNLOAD_BYTES", str(512 * 1024 * 1024)))
DOWNLOAD_TIMEOUT_S = float(os.environ.get("MFA_DOWNLOAD_TIMEOUT_S", "120"))
MAX_BATCH_ITEMS = int(os.environ.get("MFA_MAX_BATCH_ITEMS", "64"))
BLOCK_PRIVATE_URLS = os.environ.get("MFA_BLOCK_PRIVATE_URLS", "true").lower() == "true"
WORK_ROOT = os.environ.get("MFA_WORK_DIR") or None

AUDIO_KEYS = ("audio", "audio_url", "audio_base64", "url", "gcs_uri")
VALID_FORMATS = {"json", "textgrid", "words", "phones"}

_GCS_CLIENT = None
_MFA_INFO: Dict[str, Any] = {}
_INFO_LOCK = threading.Lock()


class InputError(ValueError):
    """Bad job input — caller's fault, not retryable."""


# --------------------------------------------------------------------------- #
#                                   Runtime                                    #
# --------------------------------------------------------------------------- #
def _run(cmd: List[str], timeout: float, env: Optional[Dict[str, str]] = None
         ) -> subprocess.CompletedProcess:
    merged = {**os.environ, **(env or {})}
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=merged)


def mfa_info() -> Dict[str, Any]:
    """Version and model inventory. Cached after the first successful probe."""
    global _MFA_INFO
    with _INFO_LOCK:
        if _MFA_INFO:
            return dict(_MFA_INFO)
        info: Dict[str, Any] = {
            "acoustic_model": ACOUSTIC_MODEL,
            "dictionary": DICTIONARY,
            "num_jobs": NUM_JOBS,
            "cpu_count": os.cpu_count(),
        }
        try:
            p = _run(["mfa", "version"], timeout=120)
            info["mfa_version"] = (p.stdout or p.stderr).strip().splitlines()[-1] if p.returncode == 0 else "unknown"
        except Exception as e:
            info["mfa_version"] = f"probe failed: {type(e).__name__}"
        _MFA_INFO = info
        return dict(info)


def is_ready() -> bool:
    """True when the MFA binary answers and the pretrained models are present."""
    try:
        if _run(["mfa", "version"], timeout=120).returncode != 0:
            return False
        p = _run(["mfa", "model", "list", "acoustic"], timeout=180)
        return p.returncode == 0 and ACOUSTIC_MODEL in (p.stdout or "")
    except Exception:
        return False


def warmup() -> Dict[str, Any]:
    """Touch the binary and model registry so the first request doesn't."""
    info = mfa_info()
    info["ready"] = is_ready()
    return info


# --------------------------------------------------------------------------- #
#                                    Audio                                     #
# --------------------------------------------------------------------------- #
def _assert_public_host(url: str) -> None:
    """Reject URLs resolving to private/loopback ranges (SSRF guard)."""
    if not BLOCK_PRIVATE_URLS:
        return
    host = urlparse(url).hostname
    if not host:
        raise InputError("URL has no host.")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise InputError(f"Could not resolve host {host!r}: {e}") from e
    for i in infos:
        ip = ipaddress.ip_address(i[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise InputError(
                f"Refusing to fetch {host!r}: resolves to non-public address {ip}. "
                "Set MFA_BLOCK_PRIVATE_URLS=false if this is intentional."
            )


def _download(url: str, dest: str) -> int:
    _assert_public_host(url)
    written = 0
    with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT_S,
                      headers={"User-Agent": "mfa-aligner/1.0"}) as resp:
        resp.raise_for_status()
        declared = resp.headers.get("Content-Length")
        if declared and int(declared) > MAX_DOWNLOAD_BYTES:
            raise InputError(f"Audio is {int(declared)} bytes, over the {MAX_DOWNLOAD_BYTES} limit.")
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                if not chunk:
                    continue
                written += len(chunk)
                if written > MAX_DOWNLOAD_BYTES:
                    raise InputError(f"Audio exceeded the {MAX_DOWNLOAD_BYTES} byte limit while streaming.")
                f.write(chunk)
    if written == 0:
        raise InputError(f"Downloaded 0 bytes from {url}")
    return written


def _download_gcs(uri: str, dest: str) -> int:
    """Fetch gs://bucket/object using the runtime service account."""
    try:
        from google.cloud import storage  # type: ignore
    except ImportError as e:
        raise InputError(
            "gs:// URIs need google-cloud-storage, which is not installed in this "
            "image. Pass an https:// URL (a signed URL works) or base64 audio."
        ) from e

    global _GCS_CLIENT
    bucket_name, _, blob_name = uri[len("gs://"):].partition("/")
    if not bucket_name or not blob_name:
        raise InputError(f"Malformed GCS URI {uri!r}; expected gs://bucket/path/to/audio.wav")
    if _GCS_CLIENT is None:
        try:
            _GCS_CLIENT = storage.Client()
        except Exception as e:
            raise InputError(f"Could not initialize a GCS client: {type(e).__name__}: {e}") from e

    blob = _GCS_CLIENT.bucket(bucket_name).blob(blob_name)
    try:
        blob.reload()
    except Exception as e:
        raise InputError(f"Could not read {uri}: {type(e).__name__}: {e}") from e
    if blob.size and blob.size > MAX_DOWNLOAD_BYTES:
        raise InputError(f"{uri} is {blob.size} bytes, over the {MAX_DOWNLOAD_BYTES} limit.")
    try:
        blob.download_to_filename(dest)
    except Exception as e:
        raise InputError(f"Download of {uri} failed: {type(e).__name__}: {e}") from e
    size = os.path.getsize(dest)
    if size == 0:
        raise InputError(f"{uri} is empty.")
    return size


def _write_base64(payload: str, dest: str) -> int:
    payload = re.sub(r"\s+", "", payload)
    payload += "=" * (-len(payload) % 4)
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as e:
        raise InputError(f"Input is not valid base64: {e}") from e
    if not raw:
        raise InputError("Decoded base64 audio is empty.")
    if len(raw) > MAX_DOWNLOAD_BYTES:
        raise InputError(f"Decoded audio is {len(raw)} bytes, over the {MAX_DOWNLOAD_BYTES} limit.")
    with open(dest, "wb") as f:
        f.write(raw)
    return len(raw)


def _probe_duration(path: str) -> float:
    try:
        out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "json", path], timeout=120)
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception:
        return 0.0


def _transcode(src: str, dest: str) -> None:
    """Force 16 kHz / mono / 16-bit PCM WAV — what the pretrained models expect."""
    proc = _run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                 "-i", src, "-vn", "-map", "0:a:0",
                 "-ac", "1", "-ar", str(SAMPLE_RATE), "-acodec", "pcm_s16le", dest],
                timeout=1800)
    if proc.returncode != 0 or not os.path.exists(dest) or os.path.getsize(dest) == 0:
        raise InputError(f"ffmpeg could not decode the audio: {(proc.stderr or '').strip()[-400:] or '<no output>'}")


def _materialize(value: str, dest_wav: str, scratch: str) -> Tuple[float, str, int]:
    """Fetch from any supported source and normalize. Returns (duration, kind, bytes)."""
    raw = scratch
    if value.startswith(("http://", "https://")):
        n, kind = _download(value, raw), "url"
    elif value.startswith("gs://"):
        n, kind = _download_gcs(value, raw), "gcs"
    elif value.startswith("data:"):
        if ";base64" not in value.split(",", 1)[0]:
            raise InputError("Only base64-encoded data: URIs are supported.")
        n, kind = _write_base64(value.split(",", 1)[1], raw), "data-uri"
    elif value.startswith(("file://", "/")):
        raise InputError("Local file paths are not accepted. Pass an http(s) URL, a gs:// URI, or base64 audio.")
    else:
        n, kind = _write_base64(value, raw), "base64"

    _transcode(raw, dest_wav)
    duration = _probe_duration(dest_wav)
    if duration <= 0:
        raise InputError("Audio contains no decodable samples.")
    if duration > MAX_AUDIO_DURATION_S:
        raise InputError(f"Audio is {duration:.0f}s, over the {MAX_AUDIO_DURATION_S:.0f}s limit.")
    try:
        os.unlink(raw)
    except OSError:
        pass
    return duration, kind, n


# --------------------------------------------------------------------------- #
#                              Output parsing                                  #
# --------------------------------------------------------------------------- #
def _intervals_from_json(doc: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Pull word/phone tiers out of MFA's JSON export.

    MFA's schema has shifted across releases, so this walks the structure
    looking for tier-shaped data rather than assuming one exact layout.
    """
    out: Dict[str, List[Dict[str, Any]]] = {"words": [], "phones": []}
    tiers = doc.get("tiers") or doc.get("Tiers") or {}
    if isinstance(tiers, dict):
        for name, tier in tiers.items():
            key = "words" if "word" in str(name).lower() else "phones" if "phone" in str(name).lower() else None
            if key is None:
                continue
            entries = tier.get("entries") if isinstance(tier, dict) else tier
            for e in entries or []:
                try:
                    if isinstance(e, dict):
                        start, end, label = e.get("start"), e.get("end"), e.get("label")
                    else:  # [start, end, label]
                        start, end, label = e[0], e[1], e[2]
                    label = str(label).strip()
                    if not label:
                        continue
                    out[key].append({"start": round(float(start), 3),
                                     "end": round(float(end), 3), "text": label})
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
    return out


_TG_ITEM = re.compile(r'name\s*=\s*"([^"]+)"', re.I)
_TG_INTERVAL = re.compile(
    r"xmin\s*=\s*([\d.]+)\s*xmax\s*=\s*([\d.]+)\s*text\s*=\s*\"([^\"]*)\"", re.I | re.S)


def _intervals_from_textgrid(text: str) -> Dict[str, List[Dict[str, Any]]]:
    """Fallback parser for long-form TextGrid, used when JSON export is absent."""
    out: Dict[str, List[Dict[str, Any]]] = {"words": [], "phones": []}
    # Split into tiers on the name = "..." markers, keeping each tier's body.
    parts = re.split(r'name\s*=\s*"', text)
    for part in parts[1:]:
        name, _, body = part.partition('"')
        key = "words" if "word" in name.lower() else "phones" if "phone" in name.lower() else None
        if key is None:
            continue
        for m in _TG_INTERVAL.finditer(body):
            label = m.group(3).strip()
            if not label:
                continue
            out[key].append({"start": round(float(m.group(1)), 3),
                             "end": round(float(m.group(2)), 3), "text": label})
    return out


def _read_alignment(output_dir: str, stem: str) -> Dict[str, List[Dict[str, Any]]]:
    """Find and parse whatever MFA wrote for this utterance."""
    for root, _dirs, files in os.walk(output_dir):
        for fn in files:
            if os.path.splitext(fn)[0] != stem:
                continue
            path = os.path.join(root, fn)
            ext = os.path.splitext(fn)[1].lower()
            try:
                if ext == ".json":
                    with open(path, encoding="utf-8") as f:
                        return _intervals_from_json(json.load(f))
                if ext == ".textgrid":
                    with open(path, encoding="utf-8", errors="replace") as f:
                        return _intervals_from_textgrid(f.read())
            except Exception as e:
                print(f"[parse] {fn}: {type(e).__name__}: {e}")
    return {"words": [], "phones": []}


# --------------------------------------------------------------------------- #
#                                 Alignment                                    #
# --------------------------------------------------------------------------- #
def _write_config(workdir: str) -> str:
    """Beam widths go in a config file, never on the command line.

    `mfa align` is declared with ignore_unknown_options + allow_extra_args, so
    an unrecognised `--beam 10` does not error — Click routes `--beam` to extra
    args and then consumes `10` as the next positional, i.e. as
    CORPUS_DIRECTORY. The failure surfaces as "Corpus directory does not exist",
    pointing nowhere near the real cause. --config_path is the supported route.
    """
    path = os.path.join(workdir, "align_config.yaml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"beam: {BEAM}\nretry_beam: {RETRY_BEAM}\n")
    return path


def _run_align(corpus_dir: str, output_dir: str, config_path: Optional[str] = None) -> Tuple[bool, str]:
    """Invoke MFA over a whole corpus directory in one go.

    Only flags MFA actually declares may appear here — see _write_config for
    why an unknown option is worse than an error.
    """
    cmd = [
        "mfa", "align", "--clean", "--quiet", "--overwrite",
        "--num_jobs", str(NUM_JOBS),
        "--output_format", "json",
    ]
    if SINGLE_SPEAKER:
        cmd.append("--single_speaker")
    if config_path:
        cmd += ["--config_path", config_path]
    cmd += [corpus_dir, DICTIONARY, ACOUSTIC_MODEL, output_dir]
    try:
        p = _run(cmd, timeout=ALIGN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return False, f"mfa align exceeded {ALIGN_TIMEOUT_S:.0f}s"
    if p.returncode != 0:
        tail = ((p.stderr or "") + (p.stdout or "")).strip()[-800:]
        return False, f"mfa align failed (exit {p.returncode}): {tail or '<no output>'}"
    return True, ""


def _shape(res: Dict[str, Any], fmt: str) -> Dict[str, Any]:
    if fmt == "words":
        return {"words": res.get("words", [])}
    if fmt == "phones":
        return {"phones": res.get("phones", [])}
    return res


def align(inp: Dict[str, Any]) -> Dict[str, Any]:
    """Force-align one or more (audio, transcript) pairs.

    Input schema:
        audio_url | gcs_uri | audio_base64 | audio (str)  — the audio source
        transcript (str)                                  — required, the text spoken
        audio (list)                                      — batch: [{audio_url, transcript}, ...]
        response_format (str)  "json" (default) | "words" | "phones"

    Every item in a batch is aligned in a single MFA invocation, which is where
    the parallelism lives — the acoustic model loads once and --num_jobs workers
    share the corpus.

    Returns a single result, or {"results": [...], "count", "failed"} for a batch.
    """
    started = time.perf_counter()
    try:
        req = _parse_input(inp or {})
    except InputError as e:
        return {"error": str(e)}

    workdir = tempfile.mkdtemp(prefix="mfa-", dir=WORK_ROOT)
    corpus = os.path.join(workdir, "corpus")
    outdir = os.path.join(workdir, "out")
    os.makedirs(corpus, exist_ok=True)
    os.makedirs(outdir, exist_ok=True)

    n = len(req["items"])
    results: List[Optional[Dict[str, Any]]] = [None] * n
    staged: List[Dict[str, Any]] = []

    try:
        # Fetch in parallel. Downloading and transcoding are I/O and ffmpeg
        # bound, and doing them one at a time leaves every core idle for the
        # whole staging phase — measured at ~0.23s per file, which came to
        # dominate the request once batches grew past ~128 items.
        fetch_start = time.perf_counter()

        def _stage(pair):
            i, item = pair
            stem = f"utt{i:05d}"
            duration, kind, nbytes = _materialize(
                item["source"], os.path.join(corpus, f"{stem}.wav"),
                os.path.join(workdir, f"raw{i:05d}.bin"))
            # MFA pairs each audio file with a .lab of the same basename.
            with open(os.path.join(corpus, f"{stem}.lab"), "w", encoding="utf-8") as f:
                f.write(item["transcript"])
            return {"idx": i, "stem": stem, "duration": duration,
                    "kind": kind, "nbytes": nbytes}

        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
            futures = {pool.submit(_stage, (i, it)): i
                       for i, it in enumerate(req["items"])}
            for fut in as_completed(futures):
                i = futures[fut]
                try:
                    staged.append(fut.result())
                except InputError as e:
                    results[i] = {"error": str(e), "index": i}
                except Exception as e:
                    print(f"[error] item {i}: {type(e).__name__}: {e}")
                    results[i] = {"error": f"{type(e).__name__}: {e}", "index": i}
        fetch_s = time.perf_counter() - fetch_start

        if staged:
            align_start = time.perf_counter()
            ok, err = _run_align(corpus, outdir, _write_config(workdir))
            align_s = time.perf_counter() - align_start
            total_audio = sum(s["duration"] for s in staged)

            for s in staged:
                if not ok:
                    results[s["idx"]] = {"error": err, "index": s["idx"]}
                    continue
                parsed = _read_alignment(outdir, s["stem"])
                if not parsed["words"] and not parsed["phones"]:
                    results[s["idx"]] = {
                        "error": "Alignment produced no intervals — the transcript "
                                 "may not match the audio, or contains only "
                                 "out-of-vocabulary words.",
                        "index": s["idx"]}
                    continue
                out = _shape({"words": parsed["words"], "phones": parsed["phones"],
                              "duration": round(s["duration"], 3)}, req["response_format"])
                out["metrics"] = {
                    "audio_seconds": round(s["duration"], 3),
                    # The MFA run is shared, so per-item time is that run's cost
                    # apportioned by how much audio this item contributed.
                    "align_seconds": round(align_s * (s["duration"] / total_audio), 3) if total_audio else None,
                    "batch_items": len(staged),
                    "batch_align_seconds": round(align_s, 3),
                    "batch_realtime_factor": round(total_audio / align_s, 1) if align_s > 0 else None,
                    "fetch_seconds": round(fetch_s, 3),
                    "source_kind": s["kind"],
                    "input_bytes": s["nbytes"],
                    "num_jobs": NUM_JOBS,
                }
                results[s["idx"]] = out

        for i in range(n):
            if results[i] is None:
                results[i] = {"error": "internal: item produced no result", "index": i}

        total_s = round(time.perf_counter() - started, 3)
        if n == 1:
            out = dict(results[0])  # type: ignore[arg-type]
            if "error" in out:
                return {"error": out["error"]}
            out["mfa"] = mfa_info()
            out["total_seconds"] = total_s
            return out
        return {
            "results": results,
            "count": n,
            "failed": sum(1 for r in results if r and "error" in r),
            "mfa": mfa_info(),
            "total_seconds": total_s,
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------- #
#                                 Validation                                   #
# --------------------------------------------------------------------------- #
def _extract_source(item: Dict[str, Any], where: str) -> str:
    present = [k for k in AUDIO_KEYS if item.get(k)]
    if not present:
        raise InputError(f"{where}: provide one of {list(AUDIO_KEYS)} "
                         "(an http(s) URL, a gs:// URI, a data: URI, or base64 audio).")
    if len(present) > 1:
        raise InputError(f"{where}: got multiple audio sources {present}; supply exactly one.")
    value = item[present[0]]
    if not isinstance(value, str) or not value.strip():
        raise InputError(f"{where}: '{present[0]}' must be a non-empty string.")
    return value.strip()


def _extract_transcript(item: Dict[str, Any], where: str) -> str:
    t = item.get("transcript") or item.get("text")
    if not isinstance(t, str) or not t.strip():
        raise InputError(
            f"{where}: 'transcript' is required and must be a non-empty string. "
            "Forced alignment needs the text that was spoken — it does not "
            "transcribe audio. Use an ASR service first if you don't have one."
        )
    return " ".join(t.split())


def _parse_input(inp: Dict[str, Any]) -> Dict[str, Any]:
    raw = inp.get("audio")
    items: List[Dict[str, str]] = []
    if isinstance(raw, list):
        for i, entry in enumerate(raw):
            if not isinstance(entry, dict):
                raise InputError(f"audio[{i}] must be an object with an audio source and a transcript.")
            items.append({"source": _extract_source(entry, f"audio[{i}]"),
                          "transcript": _extract_transcript(entry, f"audio[{i}]")})
    else:
        items.append({"source": _extract_source(inp, "input"),
                      "transcript": _extract_transcript(inp, "input")})

    if len(items) > MAX_BATCH_ITEMS:
        raise InputError(f"Batch of {len(items)} exceeds the limit of {MAX_BATCH_ITEMS}.")

    fmt = str(inp.get("response_format") or "json").lower()
    if fmt not in VALID_FORMATS:
        raise InputError(f"Unknown response_format {fmt!r}; valid: {sorted(VALID_FORMATS)}")

    return {"items": items, "response_format": fmt}
