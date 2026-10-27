"""Application entry point for the ASTon migration fixture."""

import os
from openai import OpenAI

from services.chat_service import ask_assistant
from utils.legacy_embeddings import embed_text

# Legacy module-level SDK configuration. These assignments are intentional
# migration targets for the demo, not recommended for new applications.
api_key = os.getenv("OPENAI_API_KEY")
api_base = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")

client = OpenAI(api_key=api_key, base_url=api_base)

def main() -> None:
    prompt = "Explain why dependency migrations should be tested."
    answer = ask_assistant(prompt)
    vector = embed_text(prompt)

    print("Assistant response:")
    print(answer)
    print(f"Embedding dimensions: {len(vector)}")

if __name__ == "__main__":
    main()