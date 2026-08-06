# MFA Aligner API — Integration Guide

Word- and phone-level **forced alignment** on Google Cloud Run. Send audio plus the transcript you already have; get back exact timings for every word and phone.

---

## Read this first: it is not speech recognition

This service **does not transcribe audio**. It requires a transcript and works out *when* each word was spoken. A request without `transcript` is rejected with a `400`.

| | ASR (Qwen3-ASR) | Forced alignment (this) |
| --- | --- | --- |
| Input | audio | audio **+ transcript** |
| Output | text | word/phone **timings** |
| Phone-level output | no | **yes** |

If you only have audio, run ASR first and feed its text in here.

---

## Endpoint

| | |
| --- | --- |
| **Base URL** | `https://mfa-aligner-qdzv24yrua-uc.a.run.app` |
| **Region / Project** | `us-central1` / `foundary-gcp` |
| **Auth** | Google IAM — OIDC identity token |
| **Aligner** | Montreal Forced Aligner 3.4.2 (Kaldi) |
| **Models** | `english_mfa` acoustic + dictionary |
| **Hardware** | 8 vCPU, 32 GiB, **no GPU** |
| **Concurrency** | 1 request per instance, autoscales 0 → 30 |
| **Max request duration** | 3600s |
| **Max items per request** | **1024** |

Scales to zero when idle, so the first request after a quiet period takes **60–90 seconds** while an instance starts and MFA's database boots. Set client timeouts accordingly.

---

## ⚡ Batching is worth ~460× — read before writing any code

MFA carries a **fixed cost of ~39 seconds per invocation** (loading the acoustic model, preparing the corpus database). That cost is paid **once per request**, no matter how many files are in it.

Measured on this deployment, aligning 5.86s clips:

| Items per request | Total time | Throughput | Per file |
| --- | --- | --- | --- |
| **1** | 60.9s | **0.1× realtime** | 60.9s |
| 32 | ~39s | 4.8× | 1.22s |
| 128 | 69.3s | 10.8× | 0.54s |
| 256 | 52.1s | 28.8× | 0.20s |
| **512** | **64.8s** | **46.3×** | **0.13s** |

**One file per request is ~460× slower than 512 per request, for identical audio.**

> **Send 512 items per request.** This is the single most important thing in this document. A loop that posts one file at a time will turn a two-minute job into a multi-day one and bill accordingly.

The alignment itself is nearly flat with batch size (`align_seconds ≈ 39 + 0.0046 × audio_seconds`), so throughput keeps improving well past 512. Use larger batches if your files are short; keep total audio per request under ~45 minutes to stay clear of the 3600s timeout.

### Matching client concurrency

Each instance handles **one** request at a time — MFA already saturates all 8 cores internally, so a second concurrent request would just contend for the same CPUs.

```text
client concurrency = max-instances = 30
```

Send up to **30 requests in parallel**. Fewer, and you leave instances unstarted.

### Sizing a bulk run

```text
batches      = N / 512
per batch   ≈ 39 + 0.0046 × (512 × avg_duration)  seconds
wall clock  ≈ batches / instances × per_batch
cost         = $0.000208 per instance-second (8 vCPU + 32 GiB)
```

Worked example — **12,000 files averaging 19s**: 24 batches, ~96s each, 24 in parallel → **~2 minutes, under $1**.

---

## Authentication

Cloud Run requires an **OIDC identity token** — *not* an OAuth access token. The `audience` must be the service base URL. This is the most common integration mistake.

The service account `asr-client@foundary-gcp.iam.gserviceaccount.com` already has `roles/run.invoker`. **The same key file used for Qwen3-ASR works here** — one credential, both services.

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/secure/path/asr-client.json
```

The key is supplied privately by the project owner. It is not in this repository and must never be committed — **this repo is public**.

If your backend runs on GCP, skip the key file: attach the `asr-client` service account to the workload and the libraries resolve credentials from the metadata server.

Tokens last **1 hour**; the client libraries below cache and refresh automatically.

---

## `POST /align`

### Single item

```json
{
  "audio_url": "https://example.com/speech.wav",
  "transcript": "mister quilter is the apostle of the middle classes"
}
```

Supply exactly one audio source — `audio_url`, `gcs_uri`, `audio_base64`, or `audio` — plus `transcript` (alias: `text`). Anything ffmpeg can decode is accepted and normalised to 16 kHz mono internally.

`response_format`: `json` (default, words + phones), `words`, or `phones`.

### Response

```json
{
  "words": [
    { "start": 0.53, "end": 0.79, "text": "mister" },
    { "start": 0.79, "end": 1.31, "text": "quilter" }
  ],
  "phones": [
    { "start": 0.53, "end": 0.61, "text": "mʲ" },
    { "start": 0.61, "end": 0.68, "text": "i" }
  ],
  "duration": 5.855,
  "metrics": {
    "audio_seconds": 5.855,
    "align_seconds": 2.1,
    "batch_items": 512,
    "batch_align_seconds": 52.8,
    "batch_realtime_factor": 46.3,
    "fetch_seconds": 11.7,
    "num_jobs": 8
  }
}
```

Phones are IPA labels from the `english_mfa` dictionary. Times are seconds from the start of the audio.

### Batch — the normal way to use this

```json
{
  "audio": [
    { "gcs_uri": "gs://bucket/a.wav", "transcript": "first utterance" },
    { "gcs_uri": "gs://bucket/b.wav", "transcript": "second utterance" }
  ],
  "response_format": "words"
}
```

```json
{
  "results": [
    { "words": [ ], "metrics": { } },
    { "error": "Alignment produced no intervals ...", "index": 1 }
  ],
  "count": 2,
  "failed": 1
}
```

Results are in **input order**. One bad item does not fail the batch — check each entry for an `error` key.

### Prefer `gs://` for volume

Base64 inflates payloads ~33%, and at 512 items per request that matters. Put audio in Cloud Storage and pass `gs://` URIs; the service reads them with its own identity. Grant the bucket:

```bash
gcloud storage buckets add-iam-policy-binding gs://your-bucket \
  --member=serviceAccount:mfa-aligner-sa@foundary-gcp.iam.gserviceaccount.com \
  --role=roles/storage.objectViewer
```

---

## Code

### Python

```python
import google.auth.transport.requests, google.oauth2.id_token, requests

URL = "https://mfa-aligner-qdzv24yrua-uc.a.run.app"

def token() -> str:
    """OIDC identity token. Audience MUST be the service base URL."""
    return google.oauth2.id_token.fetch_id_token(
        google.auth.transport.requests.Request(), URL)

def align_batch(items, response_format="json"):
    """items: [{'gcs_uri': ..., 'transcript': ...}, ...] — send 512 at a time."""
    r = requests.post(f"{URL}/align",
                      headers={"Authorization": f"Bearer {token()}"},
                      json={"audio": items, "response_format": response_format},
                      timeout=3600)   # cold start ~90s; large batches ~2 min
    r.raise_for_status()
    return r.json()["results"]

batch = [{"gcs_uri": f"gs://bucket/clip{i}.wav", "transcript": texts[i]}
         for i in range(512)]
for res in align_batch(batch):
    if "error" in res:
        continue
    print(res["words"][0]["start"], res["words"][0]["text"])
```

`pip install google-auth requests`

### Node.js

```js
const { GoogleAuth } = require('google-auth-library');
const URL = 'https://mfa-aligner-qdzv24yrua-uc.a.run.app';
const auth = new GoogleAuth();

async function alignBatch(items) {
  const client = await auth.getIdTokenClient(URL);   // token cached internally
  const { data } = await client.request({
    url: `${URL}/align`,
    method: 'POST',
    data: { audio: items, response_format: 'words' },
    timeout: 3600000,
  });
  return data.results;
}
```

`npm install google-auth-library`

### File upload

```bash
curl -X POST "$URL/v1/align/upload" \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  -F file=@speech.wav \
  -F transcript="the words that were spoken"
```

Single-file only — use `POST /align` with a batch for anything at volume.

### Bulk client

`batch_align.py` in this repo handles batching, retries, and resume from a JSONL manifest:

```bash
python batch_align.py --service-url "$URL" \
    --input manifest.jsonl --output aligned.jsonl \
    --batch-size 512 --concurrency 30
```

Manifest format — one object per line:

```json
{"gcs_uri": "gs://bucket/a.wav", "transcript": "the words spoken"}
```

Re-running skips anything already in the output file, so an interrupted run resumes rather than re-billing completed work.

---

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/align`, `/v1/align` | **Primary.** JSON in, JSON out. |
| `POST` | `/v1/align/upload` | Multipart upload + `transcript` form field. |
| `GET` | `/health` | Liveness. Never shells out to MFA. |
| `GET` | `/ready` | 503 until MFA and its database answer. |
| `POST` | `/warmup` | Blocks until ready. Call before a burst. |
| `GET` | `/docs` | OpenAPI docs. |

## Errors

| Status | Meaning | Action |
| --- | --- | --- |
| `400` | Missing/multiple audio sources, **missing transcript**, undecodable audio, or alignment produced no intervals. Body: `{"error": "..."}` | Fix the request. Do not retry. |
| `401` | Missing or invalid token. | Check the `Authorization` header. |
| `403` | Caller lacks `roles/run.invoker`, or the token expired (1 hour). | Grant the role or refresh. |
| `413` | Upload over 512 MB. | Split the audio. |
| `503` | All slots busy, or MFA still starting. | Retry with backoff. |
| `504` | Request exceeded 3600s. | Use smaller batches. |

Retry `429`, `503`, and network errors with exponential backoff. Never retry `400` — it will fail identically and still costs compute.

**`Alignment produced no intervals`** is the one you will actually hit: it means the transcript doesn't match the audio, or every word is out-of-vocabulary. Check the text and the language.

## Limits

| Limit | Value |
| --- | --- |
| Items per request | 1024 (**use 512**) |
| Concurrent requests | 30 (= max instances) |
| Max audio duration | 1 hour per file |
| Max upload / download | 512 MB per file |
| Max request duration | 3600s |

## Operational notes

- **Cold starts are normal.** Scale-to-zero means no idle cost, at the price of a 60–90s first request. Call `POST /warmup` ahead of a known burst.
- **Timeouts:** a 30s client timeout will fail on every cold start. Use ≥ 120s, and ≥ 600s for large batches.
- **Idempotency:** no request de-duplication. A retry re-aligns and re-bills.
- **Logs:** every request emits a structured line with `request_id`, `seconds`, `count`, and `failed`. Send `X-Request-Id` and it is echoed back.
- **Transcript quality matters.** Normalise text before sending — lowercase, no punctuation, numbers spelled out. Out-of-vocabulary words are the main cause of failed alignment.

## Contacts

- Project: `foundary-gcp` · Region: `us-central1`
- Runtime service account: `mfa-aligner-sa@foundary-gcp.iam.gserviceaccount.com`
- Caller service account: `asr-client@foundary-gcp.iam.gserviceaccount.com`
- Current revision: `mfa-aligner-00004-ksk` (image `v3`)
- Source: <https://github.com/Varshneyhars/MFA-Aligner>
