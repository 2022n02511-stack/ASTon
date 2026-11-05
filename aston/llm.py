import os
from abc import ABC, abstractmethod
from aston.exceptions import LLMError

class BaseLLMProvider(ABC):
    @abstractmethod
    def generate(self, prompt: str, model: str, temperature: float = 0.0, system_prompt: str = None, response_format=None) -> str | object:
        pass


class MistralProvider(BaseLLMProvider):
    def generate(self, prompt: str, model: str, temperature: float = 0.0, system_prompt: str = None, response_format=None) -> str:
        from mistralai import Mistral
        api_key = os.getenv("MISTRAL_API_KEY")
        if not api_key:
            raise LLMError(
                "MISTRAL_API_KEY environment variable is not set.",
                details="Provider: mistral | Model: {} | Action: Export your Mistral API key using 'export MISTRAL_API_KEY=\"...\"'".format(model)
            )
        
        try:
            client = Mistral(api_key=api_key)
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            kwargs = {
                "model": model,
                "messages": messages,
                "temperature": temperature
            }
            if response_format:
                kwargs["response_format"] = {"type": "json_object"}

            response = client.chat.complete(**kwargs)
            return response.choices[0].message.content.strip()
        except Exception as exc:
            raise LLMError(
                message="Mistral API generation failed.",
                details=f"Provider: mistral | Model: {model} | Underlying Error: {type(exc).__name__}: {exc}"
            )


class OpenAIProvider(BaseLLMProvider):
    def generate(self, prompt: str, model: str, temperature: float = 0.0, system_prompt: str = None, response_format=None):
        from openai import OpenAI
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise LLMError(
                message="OPENAI_API_KEY environment variable is not set.",
                details="Provider: openai | Model: {} | Action: Export your OpenAI API key using 'export OPENAI_API_KEY=\"...\"'".format(model)
            )
        
        try:
            client = OpenAI(api_key=api_key)
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            if response_format:
                completion = client.beta.chat.completions.parse(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    response_format=response_format,
                )
                return completion.choices[0].message.parsed
            else:
                completion = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=temperature
                )
                return completion.choices[0].message.content.strip()
        except Exception as exc:
            raise LLMError(
                message="OpenAI API generation failed.",
                details=f"Provider: openai | Model: {model} | Underlying Error: {type(exc).__name__}: {exc}"
            )


class AnthropicProvider(BaseLLMProvider):
    def generate(self, prompt: str, model: str, temperature: float = 0.0, system_prompt: str = None, response_format=None) -> str:
        import anthropic
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise LLMError(
                message="ANTHROPIC_API_KEY environment variable is not set.",
                details="Provider: anthropic | Model: {} | Action: Export your Anthropic API key using 'export ANTHROPIC_API_KEY=\"...\"'".format(model)
            )
        
        try:
            client = anthropic.Anthropic(api_key=api_key)
            kwargs = {
                "model": model,
                "max_tokens": 4096,
                "temperature": temperature,
                "messages": [{"role": "user", "content": prompt}]
            }
            if system_prompt:
                sys_content = system_prompt
                if response_format:
                    sys_content += "\nProvide your response strictly as a valid JSON object matching the requested schema."
                kwargs["system"] = sys_content

            response = client.messages.create(**kwargs)
            return response.content[0].text.strip()
        except Exception as exc:
            raise LLMError(
                message="Anthropic API generation failed.",
                details=f"Provider: anthropic | Model: {model} | Underlying Error: {type(exc).__name__}: {exc}"
            )


class GoogleProvider(BaseLLMProvider):
    def generate(self, prompt: str, model: str, temperature: float = 0.0, system_prompt: str = None, response_format=None):
        from google import genai
        from google.genai import types
        
        if not (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")):
            raise LLMError(
                message="GEMINI_API_KEY (or GOOGLE_API_KEY) environment variable is not set.",
                details="Provider: google | Model: {} | Action: Export your Gemini API key using 'export GEMINI_API_KEY=\"...\"'".format(model)
            )
        
        try:
            client = genai.Client()
            config_kwargs = {"temperature": temperature}
            if system_prompt:
                config_kwargs["system_instruction"] = system_prompt

            if response_format:
                config_kwargs["response_mime_type"] = "application/json"
                config_kwargs["response_schema"] = response_format

            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(**config_kwargs)
            )
            return response.text.strip()
        except Exception as exc:
            raise LLMError(
                message="Google Gemini API generation failed.",
                details=f"Provider: google | Model: {model} | Underlying Error: {type(exc).__name__}: {exc}"
            )


_PROVIDERS = {
    "mistral": MistralProvider,
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "google": GoogleProvider,
}


def call_llm(
    prompt: str, 
    provider: str = "mistral", 
    model: str = "codestral-latest", 
    temperature: float = 0.0, 
    system_prompt: str = None,
    response_format=None
) -> str | object:
    provider_lower = provider.lower()
    if provider_lower not in _PROVIDERS:
        supported = list(_PROVIDERS.keys())
        raise LLMError(
            message=f"Unsupported LLM provider: '{provider}'",
            details=f"Requested Provider: {provider} | Supported Providers: {supported}"
        )
    
    provider_instance = _PROVIDERS[provider_lower]()
    return provider_instance.generate(
        prompt=prompt, 
        model=model, 
        temperature=temperature, 
        system_prompt=system_prompt,
        response_format=response_format
    )