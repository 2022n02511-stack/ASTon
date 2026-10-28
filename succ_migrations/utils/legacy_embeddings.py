"""Embedding helper using the legacy OpenAI SDK interface."""

import openai


def embed_text(text: str) -> list[float]:
    """Create an embedding using the pre-v1 Embedding API."""
    response = openai.Embedding.create(
        model="text-embedding-ada-002",
        input=text,
    )
    return response["data"][0]["embedding"]