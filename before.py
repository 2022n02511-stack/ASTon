import os
import openai
import requests

# 1. Non-call property and error references
api_key = openai.api_key
base_url = openai.base_url
proxy_setting = openai.proxy

# 2. Legacy OpenAI call (v0.28.1 style)
client = openai.OpenAI()
response = client.chat.completions.create(
    model="gpt-3.5-turbo",
    messages=[
        {"role": "user", "content": "Hello from legacy OpenAI"}
    ]
)

# 3. Standard requests library usage (second module)
api_response = requests.get("https://httpbin.org/get")
print(api_response.status_code)

# 4. Error handling reference
try:
    err_class = openai.BadRequestError
except Exception:
    pass