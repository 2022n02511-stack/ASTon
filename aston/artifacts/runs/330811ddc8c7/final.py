import openai
import os

openai.api_key = os.getenv("OPENAI_API_KEY")

def create_embedding(text):
    try:
        client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        response = client.embeddings.create(
            input=text,
            model="text-embedding-ada-002"
        )
        return response.data[0].embedding

    except openai.BadRequestError as exc:
        print(f"Invalid request: {exc}")
        return None

if __name__ == "__main__":
    embedding = create_embedding("Dependency migration testing")
    if embedding:
        print(f"Embedding dimensions: {len(embedding)}")