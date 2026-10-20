import os
import openai
import requests

# 1. Global module configuration (no client instantiation in v0.x)
openai.api_key = os.getenv("OPENAI_API_KEY", "test-api-key")
openai.api_base = "https://api.openai.com/v1"

# 2. Legacy OpenAI call (using flat module-level static method)
response = openai.ChatCompletion.create(
    model="gpt-3.5-turbo",
    messages=[
        {"role": "user", "content": "Hello from legacy OpenAI"}
    ]
)

print(response["choices"][0]["message"]["content"])

# 3. Standard requests library usage — remains unchanged
api_response = requests.get("https://httpbin.org/get")
print(api_response.status_code)

# 4. Legacy error handling reference (errors live under openai.error)
try:
    err_class = openai.error.InvalidRequestError
except Exception:
    pass