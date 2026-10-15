import os
from openai import OpenAI
import requests

# 1. Non-call property and error references
api_key = os.environ.get("OPENAI_API_KEY")
base_url = os.environ.get("OPENAI_BASE_URL")
proxy_setting = os.environ.get("OPENAI_PROXIES")

# 2. Legacy OpenAI call (v0.28.1 style)
client = OpenAI(api_key=api_key, base_url=base_url)
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
    from openai import BadRequestError
    err_class = BadRequestError
except Exception:
    pass