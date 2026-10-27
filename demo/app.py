"""Application entry point for the ASTon migration fixture."""

from services.chat_service import ask_assistant
from utils.legacy_embeddings import embed_text


def main() -> None:
    prompt = "Explain why dependency migrations should be tested."
    answer = ask_assistant(prompt)
    vector = embed_text(prompt)

    print("Assistant response:")
    print(answer)
    print(f"Embedding dimensions: {len(vector)}")


if __name__ == "__main__":
    main()
