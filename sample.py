import openai
import openai as oai

from openai import OpenAI
from openai import OpenAI as Client

import os
from typing import Any


# ============================================================
# 1. Global configuration
# ============================================================

openai.api_key = os.getenv("OPENAI_API_KEY")
oai.api_base = "https://example.com/v1"
# openai.proxy = "http://localhost:8080"


# ============================================================
# 2. Direct legacy module calls
# ============================================================

completion = openai.ChatCompletion.create(
    model="gpt-3.5-turbo",
    messages=[
        {"role": "user", "content": "Hello"}
    ]
)

embedding = openai.Embedding.create(
    input="hello",
    model="text-embedding-ada-002"
)


# ============================================================
# 3. Aliased module calls
# ============================================================

completion_2 = oai.ChatCompletion.create(
    model="gpt-3.5-turbo",
    messages=[
        {"role": "user", "content": "Hello again"}
    ]
)


# ============================================================
# 4. Direct imported class
# ============================================================

client = OpenAI()

response = client.chat.completions.create(
    model="gpt-4",
    messages=[
        {"role": "user", "content": "Hello from client"}
    ]
)


# ============================================================
# 5. Aliased imported class
# ============================================================

client_2 = Client()

response_2 = client_2.chat.completions.create(
    model="gpt-4",
    messages=[
        {"role": "user", "content": "Hello from alias"}
    ]
)


# ============================================================
# 6. Multiple client instances
# ============================================================

production_client = OpenAI(
    api_key="production-key"
)

test_client = OpenAI(
    api_key="test-key"
)

production_response = production_client.chat.completions.create(
    model="gpt-4",
    messages=[]
)

test_response = test_client.chat.completions.create(
    model="gpt-4",
    messages=[]
)


# ============================================================
# 7. Client stored in another variable
# ============================================================

original_client = OpenAI()

client_alias = original_client

aliased_response = client_alias.chat.completions.create(
    model="gpt-4",
    messages=[]
)


# ============================================================
# 8. Function receiving client
# ============================================================

def make_request(client: Any):
    return client.chat.completions.create(
        model="gpt-4",
        messages=[]
    )


make_request(client)


# ============================================================
# 9. Function creating client internally
# ============================================================

def create_completion():
    local_client = OpenAI()

    return local_client.chat.completions.create(
        model="gpt-4",
        messages=[]
    )


result = create_completion()


# ============================================================
# 10. Class storing client as an attribute
# ============================================================

class AIService:

    def __init__(self):
        self.client = OpenAI()

    def generate(self):
        return self.client.chat.completions.create(
            model="gpt-4",
            messages=[]
        )


service = AIService()
service.generate()


# ============================================================
# 11. Client created through a helper
# ============================================================

def get_client():
    return OpenAI()


helper_client = get_client()

helper_response = helper_client.chat.completions.create(
    model="gpt-4",
    messages=[]
)


# ============================================================
# 12. Calls inside loops
# ============================================================

for prompt in ["one", "two", "three"]:
    loop_response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "user", "content": prompt}
        ]
    )


# ============================================================
# 13. Calls inside conditionals
# ============================================================

if True:
    conditional_response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[]
    )


# ============================================================
# 14. Nested attribute access
# ============================================================

error_type = openai.InvalidRequestError

base_url = openai.api_base


# ============================================================
# 15. Keyword-heavy calls
# ============================================================

kwargs = {
    "model": "gpt-3.5-turbo",
    "messages": []
}

keyword_response = openai.ChatCompletion.create(
    **kwargs
)


# ============================================================
# 16. Star import
# ============================================================

# NOTE:
# This is intentionally included as a hard case.
# A precise static analyzer may choose to mark this as unresolved.

# from openai import *


# ============================================================
# 17. Shadowing
# ============================================================

def unrelated_function(openai):

    # This `openai` is NOT the imported package.
    return openai.ChatCompletion.create(
        model="fake-model",
        messages=[]
    )


# ============================================================
# 18. Local variable with same name as package
# ============================================================

def another_function():
    openai = SomeOtherObject()

    return openai.ChatCompletion.create(
        model="fake-model",
        messages=[]
    )


# ============================================================
# 19. Conditional import
# ============================================================

try:
    import openai as conditional_openai
except ImportError:
    conditional_openai = None

if conditional_openai:
    conditional_completion = conditional_openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[]
    )


# ============================================================
# 20. Dynamic attribute access
# ============================================================

# resource = getattr(openai, "ChatCompletion")

# dynamic_completion = resource.create(
#     model="gpt-3.5-turbo",
#     messages=[]
# )


# ============================================================
# 21. Completely unrelated API
# ============================================================

import requests

request_response = requests.get(
    "https://example.com"
)


# ============================================================
# 22. Same-looking API from another object
# ============================================================

class FakeOpenAI:

    class ChatCompletion:

        @staticmethod
        def create(**kwargs):
            return kwargs


fake = FakeOpenAI()

fake_response = fake.ChatCompletion.create(
    model="not-openai"
)


# ============================================================
# 23. Exception usage
# ============================================================

# try:
#     raise openai.InvalidRequestError(
#         "Something went wrong",
#         response=None,
#         body=None
#     )
# except openai.InvalidRequestError:
#     print("Invalid request")


# ============================================================
# 24. Nested function
# ============================================================

def outer():

    def inner():

        nested_client = OpenAI()

        return nested_client.chat.completions.create(
            model="gpt-4",
            messages=[]
        )

    return inner()


# ============================================================
# 25. Multiple unrelated packages together
# ============================================================

from pydantic import BaseModel
import requests


class Config(BaseModel):
    name: str


config = Config(name="test")

response = requests.get(
    "https://example.com"
)