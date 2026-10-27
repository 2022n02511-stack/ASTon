"""Application entry point for the ASTon migration fixture."""

import os
import openai

from services.chat_service import ask_assistant
from utils.legacy_embeddings import embed_text


# Legacy module-level SDK configuration. These assignments are intentional
# migration targets for the demo, not recommended for new applications.
openai.api_key = os.getenv("OPENAI_API_KEY")
openai.api_base = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")


def main() -> None:
    prompt = "Explain why dependency migrations should be tested."
    answer = ask_assistant(prompt)
    vector = embed_text(prompt)

    print("Assistant response:")
    print(answer)
    print(f"Embedding dimensions: {len(vector)}")


if __name__ == "__main__":
    main()
