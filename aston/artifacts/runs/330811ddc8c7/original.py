import openai
import os

openai.api_key = os.getenv("OPENAI_API_KEY")

def create_embedding(text):
    try:
        response = openai.Embedding.create(
            input=text,
            model="text-embedding-ada-002"
        )
        return response["data"][0]["embedding"]

    except openai.InvalidRequestError as exc:
        print(f"Invalid request: {exc}")
        return None

if __name__ == "__main__":
    embedding = create_embedding("Dependency migration testing")
    if embedding:
        print(f"Embedding dimensions: {len(embedding)}")
