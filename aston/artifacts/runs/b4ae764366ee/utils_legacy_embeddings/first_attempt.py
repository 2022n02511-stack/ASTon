"""Embedding helper using the legacy OpenAI SDK interface."""

import openai
from openai import OpenAI

def embed_text(text: str) -> list[float]:
    """Create an embedding using the pre-v1 Embedding API."""
    client = OpenAI()
    response = client.embeddings.create(
        model="text-embedding-ada-002",
        input=text,
    )
    return response.data[0].embedding