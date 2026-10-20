import openai
from openai import OpenAI
import requests

# 1. Legacy OpenAI configuration
openai.api_key = "test-api-key"
openai.base_url = "https://api.openai.com/v1"

# 2. Legacy OpenAI chat completion API
client = OpenAI(api_key=openai.api_key, base_url=openai.base_url)
response = client.chat.completions.create(
    model="gpt-3.5-turbo",
    messages=[
        {
            "role": "user",
            "content": "Explain dependency migration briefly."
        }
    ],
)

print(response.choices[0].message.content)

# 3. Requests API usage — should remain unchanged
api_response = requests.get(
    "https://httpbin.org/get",
    timeout=10,
)
api_response.raise_for_status()

print(api_response.status_code)
print(api_response.json().get("url"))