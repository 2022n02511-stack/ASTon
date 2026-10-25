# ASTon Multi-File Migration Demo

This small demo repository simulates a Python application that uses the legacy OpenAI Python SDK API.

## Project structure

```text
aston_multifile_demo/
├── README.md
├── requirements.txt
├── app.py
├── services/
│   └── chat_service.py
└── utils/
    └── legacy_embeddings.py
```

## Application behavior

- `app.py` configures the legacy SDK and coordinates the application.
- `services/chat_service.py` sends chat requests using the pre-v1 `openai.ChatCompletion.create` interface.
- `utils/legacy_embeddings.py` creates embeddings using the pre-v1 `openai.Embedding.create` interface.

## Migration goal

Migrate this repository from `openai==0.28.1` to the modern OpenAI Python SDK (`openai>=1.0.0`).

The source intentionally includes legacy patterns such as module-level `openai.api_key`, `openai.api_base`, `openai.ChatCompletion.create`, and `openai.Embedding.create`. Treat this as a migration fixture, not production-ready code.

## Run

Create a virtual environment, install `requirements.txt`, and run:

```bash
python app.py
```

The demo requires a valid `OPENAI_API_KEY` and network access to call the API. It is not intended to be executed against the legacy requirements after migration without credentials.
