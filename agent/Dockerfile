# Pinned by tag AND digest: rebuilds are reproducible. Bump both together.
ARG PYTHON_IMAGE=python:3.12.7-slim-bookworm@sha256:60d9996b6a8a3689d36db740b49f4327be3be09a21122bd02fb8895abb38b50d

# ---- build stage: dependencies into a venv; pip caches and wheels stay here ----
FROM ${PYTHON_IMAGE} AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install -r /tmp/requirements.txt \
 && (/opt/venv/bin/pip uninstall -y pip setuptools wheel || true)
# Bake the embedding model in, so a running Loki never downloads anything (embeddings.py).
ARG EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
RUN /opt/venv/bin/python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='${EMBEDDING_MODEL}', cache_dir='/opt/models')" \
 && chmod -R a+rX /opt/models

# ---- runtime stage: no compilers, no pip cache, non-root ----
FROM ${PYTHON_IMAGE}
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    LOKI_EMBEDDING_CACHE=/opt/models HF_HUB_OFFLINE=1 ORT_DISABLE_TELEMETRY=1
RUN groupadd --system --gid 10001 loki \
 && useradd --system --uid 10001 --gid loki --home-dir /app --shell /usr/sbin/nologin loki
COPY --from=build /opt/venv /opt/venv
COPY --from=build /opt/models /opt/models
WORKDIR /app
COPY --chown=root:root . .
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status == 200 else 1)"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
