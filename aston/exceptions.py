"""Centralized exception hierarchy for ASTon.

Maps every pipeline failure category to a distinct exception type
and standardized exit code for predictable CLI behavior.
"""

class AstonError(Exception):
    """Base exception for all ASTon pipeline errors."""
    exit_code = 1

    def __init__(self, message: str, details: str | None = None):
        super().__init__(message)
        self.message = message
        self.details = details


class ConfigurationError(AstonError):
    """Configuration or environment setup failures (e.g., missing API keys)."""
    exit_code = 2


class IngestionError(AstonError):
    """Repository discovery, documentation ingestion, or changelog fetching failures."""
    exit_code = 3


class LLMError(AstonError):
    """Mistral API rate limits, connection timeouts, or invalid parser responses."""
    exit_code = 4


class ScanningError(AstonError):
    """AST parsing, symbol indexing, or syntax inspection failures."""
    exit_code = 5


class MigrationError(AstonError):
    """Code patching, rule application, or repair loop failures."""
    exit_code = 6


class VerificationError(AstonError):
    """Docker container execution, test suite failures, or verification errors."""
    exit_code = 7


class GitHubError(AstonError):
    """Git workspace, remote resolution, authentication, or PR creation failures."""
    exit_code = 8