# MFA Aligner — Montreal Forced Aligner on Cloud Run

Word- and phone-level **forced alignment** as a serverless HTTP API. Send audio plus the transcript you already have, get back precise timings for every word and phone.

Built on [Montreal Forced Aligner](https://github.com/MontrealCorpusTools/Montreal-Forced-Aligner) (Kaldi). Deployed on Google Cloud Run, **CPU only** — see below for why that is the right choice and not a compromise.

## Forced alignment is not speech recognition

This service does **not** transcribe audio. It requires a transcript and works out *when* each word was spoken.

| | ASR (e.g. Qwen3-ASR) | Forced alignment (this) |
| --- | --- | --- |
| Input | audio | audio **+ transcript** |
| Output | text | word/phone **timings** |
| Compute | GPU | **CPU** |
| Phone-level output | no | **yes** |

If you don't have a transcript, run ASR first and feed its output here.

## Why CPU and not GPU

MFA runs on Kaldi, whose supported conda build is CPU-only (`kaldi=*=cpu*` in MFA's own install docs). Alignment is GMM-HMM Viterbi search, which parallelises across **cores**, not GPU SMs. A `kaldi-cuda` package exists but is not MFA's supported path, and MFA is [moving away from Kaldi](https://github.com/MontrealCorpusTools/Montreal-Forced-Aligner) altogether.

Attaching a GPU would bill roughly 5× more for hardware that sits idle:

| | GPU instance | **CPU instance (this)** |
| --- | --- | --- |
| Config | 8 vCPU + 1× L4 | 8 vCPU |
| Cost | ~$1.42/hour | **~$0.29/hour** |
| GPU quota consumed | 1 per instance | **none** |

Consuming no GPU quota also means this scales independently of any GPU services in the same project.

**Scale it with vCPUs** (`MFA_NUM_JOBS`, which defaults to the instance's core count) **and with instance count**.

## Quick start

```powershell
gcloud config set project foundary-gcp
.\deploy\gcp\deploy.ps1 -ProjectId foundary-gcp
```

```bash
PROJECT_ID=foundary-gcp ./deploy/gcp/deploy.sh
```

## API

### `POST /align`

```json
{
  "audio_url": "https://example.com/speech.wav",
  "transcript": "mister quilter is the apostle of the middle classes"
}
```

Response:

```json
{
  "words": [
    { "start": 0.56, "end": 0.80, "text": "mister" },
    { "start": 0.80, "end": 1.28, "text": "quilter" }
  ],
  "phones": [
    { "start": 0.56, "end": 0.68, "text": "M" },
    { "start": 0.68, "end": 0.74, "text": "IH1" }
  ],
  "duration": 5.855,
  "metrics": {
    "audio_seconds": 5.855,
    "align_seconds": 2.1,
    "batch_items": 1,
    "batch_realtime_factor": 2.8,
    "num_jobs": 8
  }
}
```

### Batch — this is where the throughput is

MFA's unit of work is a **corpus directory**, not a file. Every item in one request is aligned in a single MFA invocation: the acoustic model loads once and `--num_jobs` workers share the corpus. Sending files one per request forfeits all of that.

```json
{
  "audio": [
    { "gcs_uri": "gs://bucket/a.wav", "transcript": "first utterance" },
    { "gcs_uri": "gs://bucket/b.wav", "transcript": "second utterance" }
  ]
}
```

Up to **64 items** per request. Results come back in input order; one bad item does not fail the batch.

### Input sources

Supply exactly one of `audio_url`, `gcs_uri`, `audio_base64`, or `audio`, plus a `transcript` (alias: `text`). Anything ffmpeg can decode is accepted and normalised to 16 kHz mono internally.

### Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/align`, `/v1/align` | Primary. JSON in, JSON out. |
| `POST` | `/v1/align/upload` | Multipart file upload + `transcript` form field. |
| `GET` | `/health` | Liveness. Never shells out to MFA. |
| `GET` | `/ready` | 503 until MFA and its database answer. |
| `POST` | `/warmup` | Blocks until ready. |
| `GET` | `/docs` | OpenAPI docs. |

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `MFA_ACOUSTIC_MODEL` | `english_mfa` | Pretrained acoustic model (baked into the image) |
| `MFA_DICTIONARY` | `english_mfa` | Pronunciation dictionary |
| `MFA_NUM_JOBS` | vCPU count | **The throughput knob.** MFA parallel workers. |
| `MFA_MAX_CONCURRENCY` | `1` | Alignments per instance. Keep at 1 — MFA already saturates all cores. |
| `MFA_BEAM` / `MFA_RETRY_BEAM` | `10` / `40` | Raise if utterances fail to align |
| `MFA_SINGLE_SPEAKER` | `true` | Faster when each file has one speaker |
| `MFA_MAX_BATCH_ITEMS` | `64` | Items per request |
| `MFA_MAX_AUDIO_DURATION_S` | `3600` | Reject longer audio |
| `MFA_BLOCK_PRIVATE_URLS` | `true` | SSRF guard on `audio_url` |
| `MFA_AUTH_TOKEN` | unset | Optional shared secret; IAM is primary |

Other languages: rebuild with `--build-arg ACOUSTIC_MODEL=... --build-arg DICTIONARY=...`. See [MFA's pretrained models](https://mfa-models.readthedocs.io/).

## Architecture notes

**PostgreSQL.** MFA 3.x keeps corpus state in a PostgreSQL instance it manages itself. `entrypoint.sh` starts it before uvicorn, and `/ready` reports failure rather than crash-looping so problems are debuggable from logs.

**Concurrency 1.** MFA saturates every core it is given. A second concurrent alignment contends for the same CPUs and makes both slower. Scale out with instances, not concurrency.

**Startup probe.** MFA has to boot its database before it can align. Without a probe on `/ready`, Cloud Run routes traffic the moment the port opens and early requests fail.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `Alignment produced no intervals` | The transcript doesn't match the audio, or is entirely out-of-vocabulary. Check the text and the language of the dictionary. |
| Words missing from alignment | Out-of-vocabulary words. Add a custom dictionary, or raise `MFA_RETRY_BEAM`. |
| `/ready` returns 503 | MFA's PostgreSQL didn't start. Check logs for `[mfa-server]`. |
| `mfa align failed (exit N)` | Usually a malformed transcript or unsupported audio. The tail of MFA's stderr is included in the error. |
| Slow alignment | Raise `--cpu` on the service; `MFA_NUM_JOBS` follows it. |

## Licence

- Code: MIT
- MFA: MIT — see [upstream](https://github.com/MontrealCorpusTools/Montreal-Forced-Aligner)
- Pretrained models: check individual model licences at [mfa-models](https://mfa-models.readthedocs.io/)
