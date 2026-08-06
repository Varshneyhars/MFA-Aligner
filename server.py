"""HTTP server for Montreal Forced Aligner on Cloud Run (CPU).

MFA is CPU-bound and already parallelises internally across --num_jobs workers,
so an instance is saturated by ONE request at a time. Cloud Run is therefore
deployed with --concurrency 1 and the semaphore below enforces the same thing
from inside: a second concurrent alignment would just contend for the same cores
and make both slower.

Scale out by adding instances, not by adding concurrency.
"""
import asyncio
import base64
import json
import os
import secrets
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse

import mfa_core

# MFA saturates every core it is given. Extra concurrent alignments contend for
# the same CPUs and slow each other down, so this stays at 1 unless you have
# deliberately under-provisioned MFA_NUM_JOBS relative to the vCPU count.
MAX_CONCURRENCY = max(1, int(os.environ.get("MFA_MAX_CONCURRENCY", "1")))
_slots = threading.BoundedSemaphore(MAX_CONCURRENCY)

PRELOAD = os.environ.get("MFA_PRELOAD", "true").lower() == "true"
AUTH_TOKEN = os.environ.get("MFA_AUTH_TOKEN", "").strip()
REVISION = os.environ.get("K_REVISION", "local")
SERVICE = os.environ.get("K_SERVICE", "mfa-aligner")

_state: Dict[str, Any] = {"ready": False, "warming": False, "error": None}


def _log(severity: str, message: str, **fields: Any) -> None:
    """Structured log line — Cloud Logging parses JSON on stdout and picks up
    `severity` and `message`, so these arrive correctly levelled."""
    print(json.dumps({"severity": severity, "message": message,
                      "service": SERVICE, "revision": REVISION, **fields}, default=str), flush=True)


def _warm() -> None:
    _state["warming"] = True
    try:
        info = mfa_core.warmup()
        _state["ready"] = bool(info.get("ready"))
        _state["error"] = None if _state["ready"] else "MFA binary or pretrained models unavailable"
        _log("INFO" if _state["ready"] else "ERROR", "warmup complete", **info)
    except Exception as e:
        _state["error"] = f"{type(e).__name__}: {e}"
        _log("ERROR", "warmup failed", error=_state["error"])
    finally:
        _state["warming"] = False


@asynccontextmanager
async def _lifespan(_: FastAPI):
    _log("INFO", "starting", acoustic_model=mfa_core.ACOUSTIC_MODEL,
         dictionary=mfa_core.DICTIONARY, num_jobs=mfa_core.NUM_JOBS,
         concurrency=MAX_CONCURRENCY)
    if PRELOAD:
        # Background thread so uvicorn binds $PORT immediately; Cloud Run kills
        # an instance that hasn't opened its port within the startup timeout.
        threading.Thread(target=_warm, name="mfa-warm", daemon=True).start()
    yield
    _log("INFO", "shutting down")


app = FastAPI(
    title="Montreal Forced Aligner",
    description="Word- and phone-level forced alignment of audio against a known transcript.",
    version="1.0.0",
    docs_url="/docs",
    lifespan=_lifespan,
)


def _check_auth(request: Request) -> None:
    """Shared-secret check, when one is configured. Cloud Run IAM is primary."""
    if not AUTH_TOKEN:
        return
    header = request.headers.get("authorization", "")
    presented = header[7:].strip() if header.lower().startswith("bearer ") else \
        request.headers.get("x-api-key", "").strip()
    if not presented or not secrets.compare_digest(presented, AUTH_TOKEN):
        raise HTTPException(status_code=401, detail="Missing or invalid bearer token.")


# --------------------------------------------------------------------------- #
#                                   Health                                     #
# --------------------------------------------------------------------------- #
@app.get("/")
def root() -> Dict[str, Any]:
    return {
        "service": SERVICE,
        "revision": REVISION,
        "purpose": "forced alignment (audio + transcript -> word/phone timings)",
        "acoustic_model": mfa_core.ACOUSTIC_MODEL,
        "dictionary": mfa_core.DICTIONARY,
        "num_jobs": mfa_core.NUM_JOBS,
        "ready": _state["ready"],
        "endpoints": ["/align", "/v1/align", "/health", "/ready", "/warmup", "/docs"],
    }


@app.get("/health")
def health() -> Dict[str, str]:
    """Liveness. Never shells out to MFA — a long alignment in flight must not
    look like a hung instance."""
    return {"status": "ok"}


@app.get("/ready")
def ready(response: Response) -> Dict[str, Any]:
    """Readiness. 503 until the MFA binary and pretrained models answer."""
    if _state["ready"]:
        return {"status": "ready", "mfa": mfa_core.mfa_info()}
    response.status_code = 503
    return {"status": "warming" if _state["warming"] else "cold", "error": _state["error"]}


@app.post("/warmup")
def warmup_endpoint() -> Dict[str, Any]:
    if not _state["ready"]:
        _warm()
    if not _state["ready"]:
        raise HTTPException(status_code=503, detail=_state["error"] or "not ready")
    return {"status": "ready", "mfa": mfa_core.mfa_info()}


# --------------------------------------------------------------------------- #
#                                 Alignment                                    #
# --------------------------------------------------------------------------- #
def _run_blocking(payload: Dict[str, Any]) -> Dict[str, Any]:
    acquired = _slots.acquire(timeout=float(os.environ.get("MFA_QUEUE_TIMEOUT_S", "900")))
    if not acquired:
        raise HTTPException(status_code=503, detail="Server busy; all alignment slots are in use.")
    try:
        return mfa_core.align(payload)
    finally:
        _slots.release()


async def _align_async(payload: Dict[str, Any], request_id: str) -> Dict[str, Any]:
    started = time.perf_counter()
    # mfa_core shells out to Kaldi and blocks; off the event loop it goes so
    # /health stays responsive during a long corpus alignment.
    result = await asyncio.to_thread(_run_blocking, payload)
    _log("ERROR" if "error" in result else "INFO",
         "align failed" if "error" in result else "align complete",
         request_id=request_id, seconds=round(time.perf_counter() - started, 3),
         count=result.get("count", 1), failed=result.get("failed", 0),
         error=result.get("error"))
    return result


def _unwrap(body: Any) -> Dict[str, Any]:
    """Accept both {"input": {...}} and a flat {...} body."""
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
    inner = body.get("input")
    return inner if isinstance(inner, dict) else body


async def _read_json(request: Request) -> Dict[str, Any]:
    try:
        return _unwrap(await request.json())
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {e}") from e


@app.post("/align")
@app.post("/v1/align")
async def align_endpoint(request: Request):
    """Force-align audio against a transcript.

    Body: {"audio_url": "...", "transcript": "the words that were spoken"}
    Batch: {"audio": [{"audio_url": "...", "transcript": "..."}, ...]}
    """
    _check_auth(request)
    payload = await _read_json(request)
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex

    result = await _align_async(payload, request_id)
    if "error" in result:
        return JSONResponse(status_code=400, content=result)
    return JSONResponse(content=result, headers={"x-request-id": request_id})


@app.post("/v1/align/upload")
async def align_upload(
    request: Request,
    file: UploadFile = File(...),
    transcript: str = Form(...),
    response_format: str = Form(default="json"),
):
    """Multipart variant — post the audio file directly instead of base64."""
    _check_auth(request)
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(raw) > mfa_core.MAX_DOWNLOAD_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"Upload is {len(raw)} bytes, over the {mfa_core.MAX_DOWNLOAD_BYTES} limit.")

    result = await _align_async({
        "audio_base64": base64.b64encode(raw).decode("ascii"),
        "transcript": transcript,
        "response_format": response_format,
    }, request_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return JSONResponse(content=result, headers={"x-request-id": request_id})


@app.exception_handler(mfa_core.InputError)
async def _input_error_handler(request: Request, exc: mfa_core.InputError):
    return JSONResponse(status_code=400, content={"error": str(exc)})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")),
                # Cloud Run's front end holds connections open; a short
                # keep-alive makes it reuse a socket we already closed.
                timeout_keep_alive=int(os.environ.get("KEEP_ALIVE_S", "620")),
                access_log=False, log_level=os.environ.get("LOG_LEVEL", "info"))
