"""Embedding helper using the legacy OpenAI SDK interface."""

from openai import OpenAI
import os

client = OpenAI(api_key=os.environ.get('OPENAI_API_KEY'))

def embed_text(text: str) -> list[float]:
    """Create an embedding using the pre-v1 Embedding API."""
    response = client.embeddings.create(
        model="text-embedding-ada-002",
        input=text,
    )
    return response.data[0].embedding