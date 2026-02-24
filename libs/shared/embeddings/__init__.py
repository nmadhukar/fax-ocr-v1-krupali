"""Sentence-transformers embedding client (all-MiniLM-L6-v2, 384 dims)."""

from libs.shared.embeddings.embedding_client import EmbeddingClient

_client: EmbeddingClient | None = None


def get_embedding_client() -> EmbeddingClient:
    """Return a process-level singleton EmbeddingClient.

    The model is loaded once on first call (~5-15 s), then cached.
    Subsequent calls are instant.
    """
    global _client
    if _client is None:
        from libs.shared.config import get_settings
        settings = get_settings()
        model_name = (
            getattr(settings, "embedder_model_name", None)
            or "sentence-transformers/all-MiniLM-L6-v2"
        )
        _client = EmbeddingClient(model_name=model_name)
    return _client
