"""Text embeddings for finding past incidents by meaning, not by shared words.

"connection refused to db" and "postgres pool exhausted" share no words but mean
nearly the same thing. A small local model (BAAI/bge-small-en-v1.5, 384 dims, about
64 MB, runs on CPU) turns each text into a vector; similar meanings get similar vectors.

Local on purpose: incident text never leaves the Loki server, and there is no
per-call cost. The model is baked into the image, so nothing is downloaded at runtime.

Env:
  LOKI_EMBEDDINGS        local | off (default off; the pilot compose turns it on)
  LOKI_EMBEDDING_MODEL   default BAAI/bge-small-en-v1.5
  LOKI_EMBEDDING_CACHE   where the model files live (the image sets /opt/models)

Anything that goes wrong (package missing, model can't load) disables embeddings for
a while and callers fall back to keyword search: a broken model never breaks an
investigation.
"""
import logging
import math
import os
import threading
import time

log = logging.getLogger("agent")

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
RETRY_AFTER_S = 600  # after a load failure, don't retry on every call

_lock = threading.Lock()
_backend = None
_failed_at = 0.0


def enabled() -> bool:
    return os.environ.get("LOKI_EMBEDDINGS", "off").strip().lower() == "local"


def model_name() -> str:
    return os.environ.get("LOKI_EMBEDDING_MODEL", "").strip() or DEFAULT_MODEL


def set_backend(backend) -> None:
    """Tests inject a fake with passage_embed(texts) and query_embed(texts)."""
    global _backend, _failed_at
    with _lock:
        _backend, _failed_at = backend, 0.0


def _load():
    global _backend, _failed_at
    with _lock:
        if _backend is not None:
            return _backend
        if _failed_at and time.monotonic() - _failed_at < RETRY_AFTER_S:
            return None
        try:
            # onnxruntime can send usage telemetry; incident data must stay on this server.
            # The env var must be set before onnxruntime is first imported.
            os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")
            try:
                import onnxruntime
                onnxruntime.disable_telemetry_events()
            except Exception:  # noqa: BLE001
                pass
            from fastembed import TextEmbedding
            started = time.monotonic()
            _backend = TextEmbedding(model_name=model_name(), cache_dir=os.environ.get("LOKI_EMBEDDING_CACHE") or None)
            log.warning("embeddings: loaded %s in %.1fs", model_name(), time.monotonic() - started)
        except Exception as e:  # noqa: BLE001 - any failure means "fall back to keywords"
            _failed_at = time.monotonic()
            log.warning("embeddings unavailable, using keyword search: %s: %s", type(e).__name__, e)
            return None
        return _backend


def _normalize(v) -> list[float]:
    v = [float(x) for x in v]
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def embed_documents(texts: list[str]) -> list[list[float]] | None:
    """Vectors for stored texts (past incidents). None when embeddings are unavailable."""
    backend = _load() if enabled() else None
    if backend is None or not texts:
        return None if backend is None else []
    try:
        return [_normalize(v) for v in backend.passage_embed(list(texts))]
    except Exception as e:  # noqa: BLE001
        log.warning("embeddings: document embedding failed: %s", e)
        return None


def embed_query(text: str) -> list[float] | None:
    """Vector for a search query (the model adds its own query instruction)."""
    backend = _load() if enabled() else None
    if backend is None:
        return None
    try:
        return _normalize(next(iter(backend.query_embed([text]))))
    except Exception as e:  # noqa: BLE001
        log.warning("embeddings: query embedding failed: %s", e)
        return None


def similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two normalized vectors."""
    return sum(x * y for x, y in zip(a, b))


def incident_text(report: dict) -> str:
    """What a past incident is about, in the words most likely to match a new one."""
    parts = [report.get("summary") or "", report.get("root_cause") or "",
             f"service {report['root_cause_service']}" if report.get("root_cause_service") else "",
             *[str(e) for e in (report.get("evidence") or [])[:3]]]
    return "\n".join(p for p in parts if p)[:2000]
