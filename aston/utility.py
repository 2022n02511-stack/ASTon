import time
import requests
from aston.cli.console import warning

def request_with_backoff(method: str, url: str, max_retries: int = 3, backoff_factor: float = 2.0, **kwargs) -> requests.Response:
    """Execute an HTTP request with exponential backoff for transient errors."""
    delay = 1.0
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.request(method, url, timeout=20, **kwargs)
            if response.status_code < 500:
                return response
            warning(f"Server returned HTTP {response.status_code} for {url}. Retrying ({attempt}/{max_retries})...")
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt == max_retries:
                raise
            warning(f"Network request to {url} failed ({exc}). Retrying in {delay}s...")
        
        time.sleep(delay)
        delay *= backoff_factor
        
    return requests.request(method, url, timeout=20, **kwargs)