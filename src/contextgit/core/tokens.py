from dataclasses import dataclass
from typing import Any, Optional


try:
    import tiktoken
    _base_encoder = None
    _model_encoders = {}
except ImportError:
    tiktoken = None
    _base_encoder = None
    _model_encoders = {}


@dataclass(frozen=True)
class TokenEstimate:
    count: int
    source: str


def _calibrated_char_estimate(text: str) -> int:
    """Fallback estimator for environments without a tokenizer.

    The ratio is still approximate, but naming it explicitly prevents reports
    from treating char-count-derived numbers as provider tokenizer truth.
    """
    if not text:
        return 0
    return max(1, round(len(text) / 4.0))


def get_encoder(model_name: Optional[str] = None):
    global _base_encoder
    if tiktoken is None:
        return None
    if model_name:
        key = model_name.lower()
        if "gpt-4o-mini" in key:
            try:
                return tiktoken.get_encoding("cl100k_base")
            except Exception:
                pass
        if "gpt-4.1-nano" in key or "gpt-5" in key:
            try:
                return tiktoken.get_encoding("o200k_base")
            except Exception:
                pass
        if key not in _model_encoders:
            try:
                _model_encoders[key] = tiktoken.encoding_for_model(model_name)
            except Exception:
                _model_encoders[key] = None
        if _model_encoders[key] is not None:
            return _model_encoders[key]
    if _base_encoder is None:
        try:
            # o200k_base is the broadest current OpenAI-family tokenizer when
            # model-specific lookup is unavailable.
            _base_encoder = tiktoken.get_encoding("o200k_base")
        except Exception:
            try:
                _base_encoder = tiktoken.get_encoding("cl100k_base")
            except Exception:
                _base_encoder = None
    return _base_encoder


def estimate_tokens_detailed(
    text: str,
    model_name: Optional[str] = None,
    provider_tokenizer: Optional[Any] = None,
) -> TokenEstimate:
    """Estimate tokens and report the counting source.

    Source hierarchy:
    1. provider tokenizer object/callable when supplied
    2. model/model-family tokenizer via tiktoken
    3. calibrated character estimate
    """
    if not text:
        return TokenEstimate(count=0, source="empty")

    if provider_tokenizer is not None:
        try:
            if hasattr(provider_tokenizer, "count"):
                return TokenEstimate(count=int(provider_tokenizer.count(text)), source="provider_tokenizer")
            if callable(provider_tokenizer):
                return TokenEstimate(count=int(provider_tokenizer(text)), source="provider_tokenizer")
        except Exception:
            pass

    enc = get_encoder(model_name=model_name)
    if enc is not None:
        try:
            source = f"tiktoken_{enc.name}"
            return TokenEstimate(count=len(enc.encode(text)), source=source)
        except Exception:
            pass
    return TokenEstimate(count=_calibrated_char_estimate(text), source="fallback_estimate")


def token_count_source(model_name: Optional[str] = None) -> str:
    """Return the source that would be used for a non-empty text."""
    return estimate_tokens_detailed("probe", model_name=model_name).source


def estimate_tokens(text: str, model_name: Optional[str] = None) -> int:
    """Backward-compatible token-count helper returning only the count."""
    return estimate_tokens_detailed(text, model_name=model_name).count
