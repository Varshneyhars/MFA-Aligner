# Montreal Forced Aligner as a Cloud Run service. CPU only — deliberately.
#
# MFA runs on Kaldi, whose supported conda build is CPU (`kaldi=*=cpu*`). A
# kaldi-cuda package exists but is not MFA's supported path, and alignment is
# GMM-HMM Viterbi work that parallelises across cores rather than SMs. Attaching
# a GPU here would bill ~5x more for hardware that sits idle.
#
# Scale this with vCPUs (MFA --num_jobs) and with instance count.
FROM mmcauliffe/montreal-forced-aligner:latest

USER root

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080 \
    MFA_ROOT_DIR=/mfa

# ffmpeg normalises any container/codec to the 16 kHz mono WAV the pretrained
# models expect; the base image does not ship it.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# The base image puts MFA in a conda env at /env — install into that same env,
# not the system python, or the server won't see the mfa package.
ENV PATH=/env/bin:$PATH
RUN /env/bin/pip install --no-cache-dir \
        "fastapi>=0.115" \
        "uvicorn[standard]>=0.30" \
        "python-multipart>=0.0.9" \
        "requests>=2.31" \
        "google-cloud-storage>=2.18"

# Bake the pretrained models in. Cloud Run instances start from the image and
# have no persistent disk, so anything not in a layer is re-downloaded on every
# cold start.
ARG ACOUSTIC_MODEL=english_mfa
ARG DICTIONARY=english_mfa
RUN mfa model download acoustic ${ACOUSTIC_MODEL} \
 && mfa model download dictionary ${DICTIONARY} \
 && mfa model list acoustic

COPY mfa_core.py server.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

ENV MFA_ACOUSTIC_MODEL=${ACOUSTIC_MODEL} \
    MFA_DICTIONARY=${DICTIONARY} \
    MFA_PRELOAD=true \
    MFA_MAX_CONCURRENCY=1

# PostgreSQL refuses to run as root, and MFA 3.x keeps its corpus database in
# one. The base image already creates mfauser; everything MFA touches must be
# writable by it.
RUN useradd -ms /bin/bash appuser 2>/dev/null || true; \
    mkdir -p /mfa && chown -R mfauser /mfa /app 2>/dev/null || true
USER mfauser

EXPOSE 8080

# entrypoint.sh starts the MFA database before uvicorn — see the file for why
# this can't just be a CMD.
CMD ["./entrypoint.sh"]
