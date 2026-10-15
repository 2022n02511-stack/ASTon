import openai
import os

openai.api_key = os.getenv("OPENAI_API_KEY")
openai.proxy = "http://localhost:8080"

def make_request():
    return openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": "Hello"}]
    )

if __name__ == "__main__":
    print(make_request())