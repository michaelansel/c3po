"""Structured error codes and responses for C3PO coordinator."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class C3POError:
    """Structured error response."""
    code: str
    message: str
    suggestion: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON response."""
        result = {
            "error": self.message,
            "code": self.code,
        }
        if self.suggestion:
            result["suggestion"] = self.suggestion
        return result


# Error codes
class ErrorCodes:
    """C3PO error codes."""
    AGENT_NOT_FOUND = "AGENT_NOT_FOUND"
    TIMEOUT = "TIMEOUT"
    INVALID_REQUEST = "INVALID_REQUEST"
    RATE_LIMITED = "RATE_LIMITED"
    REDIS_UNAVAILABLE = "REDIS_UNAVAILABLE"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    BLOB_NOT_FOUND = "BLOB_NOT_FOUND"
    BLOB_TOO_LARGE = "BLOB_TOO_LARGE"


def agent_not_found(target: str, available: list[str]) -> C3POError:
    """Create error for unknown agent."""
    if available:
        agent_list = ", ".join(available[:5])
        if len(available) > 5:
            agent_list += f" (and {len(available) - 5} more)"
        suggestion = f"Available agents: {agent_list}"
    else:
        suggestion = "No agents are currently registered. Wait for agents to come online."

    return C3POError(
        code=ErrorCodes.AGENT_NOT_FOUND,
        message=f"Agent '{target}' not found.",
        suggestion=suggestion,
    )


def invalid_request(field: str, reason: str) -> C3POError:
    """Create error for invalid request data."""
    return C3POError(
        code=ErrorCodes.INVALID_REQUEST,
        message=f"Invalid request: {field} - {reason}",
        suggestion="Check the tool documentation for required parameters.",
    )


def rate_limited(agent_id: str, limit: int, window_seconds: int) -> C3POError:
    """Create error for rate limit exceeded."""
    return C3POError(
        code=ErrorCodes.RATE_LIMITED,
        message=f"Rate limit exceeded for agent '{agent_id}'.",
        suggestion=f"Maximum {limit} requests per {window_seconds} seconds. Wait before sending more requests.",
    )


def unauthorized(message: str = "Authentication required") -> C3POError:
    """Create error for missing or invalid authentication."""
    return C3POError(
        code=ErrorCodes.UNAUTHORIZED,
        message=message,
        suggestion="Provide a valid Authorization: Bearer <token> header.",
    )


def forbidden(agent_id: str, action: str = "access this resource") -> C3POError:
    """Create error for insufficient authorization."""
    return C3POError(
        code=ErrorCodes.FORBIDDEN,
        message=f"Agent '{agent_id}' is not authorized to {action}.",
        suggestion="Your API key does not have permission for this agent ID.",
    )


def redis_unavailable(redis_url: str, original_error: str = "") -> C3POError:
    """Create error for Redis connection failure."""
    # Parse host:port from Redis URL for clearer error message
    # URL format: redis://host:port or redis://host:port/db
    import re
    match = re.search(r"redis://([^/:]+):?(\d+)?", redis_url)
    if match:
        host = match.group(1)
        port = match.group(2) or "6379"
        location = f"{host}:{port}"
    else:
        location = redis_url

    message = f"Cannot connect to Redis at {location}."
    if original_error:
        message = f"{message} Error: {original_error}"

    return C3POError(
        code=ErrorCodes.REDIS_UNAVAILABLE,
        message=message,
        suggestion="Ensure Redis is running and accessible. Check REDIS_URL environment variable.",
    )


def blob_not_found(blob_id: str) -> C3POError:
    """Create error for blob not found."""
    return C3POError(
        code=ErrorCodes.BLOB_NOT_FOUND,
        message=f"Blob '{blob_id}' not found or has expired.",
        suggestion="Blobs expire after 24 hours. Check the blob_id and try again.",
    )


def blob_too_large(size: int, max_size: int) -> C3POError:
    """Create error for blob exceeding size limit."""
    size_mb = size / (1024 * 1024)
    max_mb = max_size / (1024 * 1024)
    return C3POError(
        code=ErrorCodes.BLOB_TOO_LARGE,
        message=f"Blob size ({size_mb:.1f}MB) exceeds maximum ({max_mb:.1f}MB).",
        suggestion="Reduce the file size or split it into smaller parts.",
    )


class RedisConnectionError(Exception):
    """Raised when Redis connection fails with actionable error message."""

    def __init__(self, redis_url: str, original_error: Exception):
        self.redis_url = redis_url
        self.original_error = original_error
        err = redis_unavailable(redis_url, str(original_error))
        super().__init__(f"{err.message} {err.suggestion}")
