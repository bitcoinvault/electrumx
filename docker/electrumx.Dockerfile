# Unit tests only. Build the root Dockerfile first and pass its local tag.
ARG RUNTIME_IMAGE=bitcoin-vault-electrumx-ci
FROM ${RUNTIME_IMAGE}

WORKDIR /checks
COPY docker/requirements-runtime.txt docker/requirements-runtime.txt
COPY requirements.txt .
RUN python -m pip install --no-cache-dir --only-binary=:all: -r requirements.txt \
    && python -m pip check
COPY tests tests
