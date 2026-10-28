"""Chat service using the legacy OpenAI SDK interface."""

import openai


def ask_assistant(prompt: str) -> str:
    """Send a prompt using the pre-v1 ChatCompletion API."""
    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "You are a concise assistant."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )
    return response["choices"][0]["message"]["content"]