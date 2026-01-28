"""Agent registration and management for C3PO coordinator."""

import json
from datetime import datetime, timezone
from typing import Optional

import redis


class AgentManager:
    """Manages agent registration and status using Redis."""

    AGENTS_KEY = "c3po:agents"
    AGENT_TIMEOUT_SECONDS = 90  # Consider agent offline after this

    def __init__(self, redis_client: redis.Redis):
        """Initialize with Redis client.

        Args:
            redis_client: Redis client instance (can be real or fakeredis)
        """
        self.redis = redis_client

    def register_agent(
        self,
        agent_id: str,
        capabilities: Optional[list[str]] = None,
    ) -> dict:
        """Register an agent or update existing registration.

        Args:
            agent_id: Unique identifier for the agent
            capabilities: Optional list of agent capabilities

        Returns:
            Agent data dict with id, capabilities, registered_at, last_seen
        """
        now = datetime.now(timezone.utc).isoformat()

        # Check if agent already exists
        existing = self.redis.hget(self.AGENTS_KEY, agent_id)
        if existing:
            agent_data = json.loads(existing)
            # Update existing agent
            agent_data["last_seen"] = now
            if capabilities is not None:
                agent_data["capabilities"] = capabilities
        else:
            # Create new agent
            agent_data = {
                "id": agent_id,
                "capabilities": capabilities or [],
                "registered_at": now,
                "last_seen": now,
            }

        self.redis.hset(self.AGENTS_KEY, agent_id, json.dumps(agent_data))
        return agent_data

    def list_agents(self) -> list[dict]:
        """List all registered agents with their status.

        Returns:
            List of agent dicts with status field added
        """
        agents_raw = self.redis.hgetall(self.AGENTS_KEY)
        agents = []

        now = datetime.now(timezone.utc)

        for agent_id, data in agents_raw.items():
            # Handle bytes from Redis
            if isinstance(agent_id, bytes):
                agent_id = agent_id.decode()
            if isinstance(data, bytes):
                data = data.decode()

            agent_data = json.loads(data)

            # Calculate status based on last_seen
            last_seen = datetime.fromisoformat(agent_data["last_seen"])
            seconds_since = (now - last_seen).total_seconds()

            if seconds_since < self.AGENT_TIMEOUT_SECONDS:
                agent_data["status"] = "online"
            else:
                agent_data["status"] = "offline"

            agents.append(agent_data)

        return agents

    def get_agent(self, agent_id: str) -> Optional[dict]:
        """Get a single agent by ID.

        Args:
            agent_id: The agent ID to look up

        Returns:
            Agent data dict or None if not found
        """
        data = self.redis.hget(self.AGENTS_KEY, agent_id)
        if data is None:
            return None

        if isinstance(data, bytes):
            data = data.decode()

        agent_data = json.loads(data)

        # Add status
        now = datetime.now(timezone.utc)
        last_seen = datetime.fromisoformat(agent_data["last_seen"])
        seconds_since = (now - last_seen).total_seconds()

        if seconds_since < self.AGENT_TIMEOUT_SECONDS:
            agent_data["status"] = "online"
        else:
            agent_data["status"] = "offline"

        return agent_data

    def update_heartbeat(self, agent_id: str) -> bool:
        """Update the last_seen timestamp for an agent.

        Args:
            agent_id: The agent ID to update

        Returns:
            True if agent exists and was updated, False otherwise
        """
        data = self.redis.hget(self.AGENTS_KEY, agent_id)
        if data is None:
            return False

        if isinstance(data, bytes):
            data = data.decode()

        agent_data = json.loads(data)
        agent_data["last_seen"] = datetime.now(timezone.utc).isoformat()

        self.redis.hset(self.AGENTS_KEY, agent_id, json.dumps(agent_data))
        return True

    def remove_agent(self, agent_id: str) -> bool:
        """Remove an agent from the registry.

        Args:
            agent_id: The agent ID to remove

        Returns:
            True if agent was removed, False if not found
        """
        result = self.redis.hdel(self.AGENTS_KEY, agent_id)
        return result > 0
