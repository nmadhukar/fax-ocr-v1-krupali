"""
VLM (Vision Language Model) integration layer.

The sole VLM implementation is LayoutLM Document QA
(impira/layoutlm-document-qa), which extracts structured fields from
document images by answering targeted natural-language questions.

Usage::

    from libs.shared.vlm import get_layoutlm_client

    client = get_layoutlm_client()
    result = client.extract(page_image, field_keys=["member_id"])
"""

from libs.shared.vlm.base import VlmClient, VlmConfig, VlmResponse

__all__ = [
    "VlmClient",
    "VlmConfig",
    "VlmResponse",
    "get_layoutlm_client",
]


def get_layoutlm_client() -> "LayoutLMClient":
    """Factory: create a LayoutLM Document QA client from application settings.

    Reads ``VlmSettings`` via ``get_settings()`` and returns a configured
    LayoutLM client.  The model is lazy-loaded on first use to avoid
    startup overhead.
    """
    from libs.shared.config import get_settings
    from libs.shared.vlm.layoutlm_client import LayoutLMClient

    settings = get_settings()
    vlm_cfg = settings.vlm

    config = VlmConfig(
        model_name=vlm_cfg.layoutlm_model_name,
        use_gpu=vlm_cfg.use_gpu,
        adapter_path=vlm_cfg.layoutlm_adapter_path,
    )

    return LayoutLMClient(config)
