"""Select the configured model provider. Tests pass a provider in directly."""

from __future__ import annotations

from apex.config import Settings
from apex.providers.anthropic import AnthropicAdapter
from apex.providers.base import ModelProvider
from apex.providers.errors import ProviderError
from apex.providers.mock import MockModelProvider
from apex.providers.openai import OpenAIAdapter


def build_provider(settings: Settings) -> ModelProvider:
    """Build the configured provider. Missing credentials fail when that provider is selected."""
    if settings.model_provider == "mock":
        return MockModelProvider()
    if settings.model_provider == "openai":
        try:
            return OpenAIAdapter(
                api_key=settings.openai_api_key,
                model=settings.openai_model,
                base_url=settings.openai_base_url,
                image_understanding=settings.openai_image_understanding,
                transcription=settings.openai_transcription,
                transcription_model=settings.openai_transcription_model,
                speech=settings.openai_speech,
                speech_model=settings.openai_speech_model,
                max_audio_bytes=settings.media_max_bytes,
            )
        except ProviderError:
            raise
    if settings.model_provider == "anthropic":
        return AnthropicAdapter(
            api_key=settings.anthropic_api_key,
            model=settings.anthropic_model,
            base_url=settings.anthropic_base_url,
            version=settings.anthropic_version,
            image_understanding=settings.anthropic_image_understanding,
        )
    raise ProviderError("unavailable", "The configured model provider is not supported.")
