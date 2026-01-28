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
    COORD_UNAVAILABLE = "COORD_UNAVAILABLE"
    AGENT_NOT_FOUND = "AGENT_NOT_FOUND"
    AGENT_BUSY = "AGENT_BUSY"
    TIMEOUT = "TIMEOUT"
    INVALID_REQUEST = "INVALID_REQUEST"
    RATE_LIMITED = "RATE_LIMITED"
    MESSAGE_EXPIRED = "MESSAGE_EXPIRED"


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


def timeout_error(operation: str, timeout_seconds: int) -> C3POError:
    """Create error for timeout."""
    return C3POError(
        code=ErrorCodes.TIMEOUT,
        message=f"Operation '{operation}' timed out after {timeout_seconds} seconds.",
        suggestion="The target agent may be offline or busy. Try again later or check agent status with list_agents.",
    )


def coordinator_unavailable(reason: str = "Connection failed") -> C3POError:
    """Create error for coordinator unavailable."""
    return C3POError(
        code=ErrorCodes.COORD_UNAVAILABLE,
        message=f"Coordinator unavailable: {reason}",
        suggestion="Check that the coordinator is running and C3PO_COORDINATOR_URL is correct.",
    )
