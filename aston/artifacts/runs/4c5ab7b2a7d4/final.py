import openai
import os
from openai import OpenAI

openai.api_key = os.getenv("OPENAI_API_KEY")
openai.proxies = "http://localhost:8080"

def make_request():
    client = OpenAI(api_key=openai.api_key, proxies=openai.proxies)
    return client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": "Hello"}]
    )

if __name__ == "__main__":
    print(make_request())