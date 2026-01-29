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
        session_id: Optional[str] = None,
        capabilities: Optional[list[str]] = None,
    ) -> dict:
        """Register an agent or update existing registration.

        Handles collision detection: if an agent with the same ID is already
        online with a different session_id, a suffix is added (agent-2, agent-3, etc.)
        to create a unique ID. The same session reconnecting keeps the same ID.

        Args:
            agent_id: Requested identifier for the agent
            session_id: Session identifier (used to detect same-session reconnects)
            capabilities: Optional list of agent capabilities

        Returns:
            Agent data dict with id, capabilities, registered_at, last_seen.
            The id field may differ from agent_id if collision was resolved.
        """
        now = datetime.now(timezone.utc).isoformat()

        # Check if agent already exists
        existing = self._get_agent_raw(agent_id)

        if existing:
            # Same session reconnecting - update heartbeat, keep same ID
            if session_id and existing.get("session_id") == session_id:
                existing["last_seen"] = now
                if capabilities is not None:
                    existing["capabilities"] = capabilities
                self.redis.hset(self.AGENTS_KEY, agent_id, json.dumps(existing))
                return self._add_status(existing)

            # Check if existing agent is online (collision!)
            last_seen = datetime.fromisoformat(existing["last_seen"])
            seconds_since = (datetime.now(timezone.utc) - last_seen).total_seconds()

            if seconds_since < self.AGENT_TIMEOUT_SECONDS:
                # Collision: different session, agent still online
                agent_id = self._resolve_collision(agent_id)

        # Create new or update offline agent
        agent_data = {
            "id": agent_id,
            "session_id": session_id,
            "capabilities": capabilities or [],
            "registered_at": now,
            "last_seen": now,
        }

        self.redis.hset(self.AGENTS_KEY, agent_id, json.dumps(agent_data))
        return self._add_status(agent_data)

    def _get_agent_raw(self, agent_id: str) -> Optional[dict]:
        """Get raw agent data without status calculation.

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

        return json.loads(data)

    def _add_status(self, agent_data: dict) -> dict:
        """Add status field to agent data based on last_seen.

        Args:
            agent_data: Agent data dict with last_seen field

        Returns:
            Agent data with status field added
        """
        now = datetime.now(timezone.utc)
        last_seen = datetime.fromisoformat(agent_data["last_seen"])
        seconds_since = (now - last_seen).total_seconds()

        if seconds_since < self.AGENT_TIMEOUT_SECONDS:
            agent_data["status"] = "online"
        else:
            agent_data["status"] = "offline"

        return agent_data

    def _resolve_collision(self, base_id: str) -> str:
        """Find next available agent ID when collision occurs.

        Tries base_id-2, base_id-3, etc. until an available ID is found.
        An ID is available if it doesn't exist or the existing agent is offline.

        Args:
            base_id: The original agent ID that collided

        Returns:
            An available agent ID with suffix
        """
        counter = 2
        while True:
            candidate = f"{base_id}-{counter}"
            existing = self._get_agent_raw(candidate)

            if existing is None:
                return candidate

            # Check if existing agent is offline (can reuse ID)
            last_seen = datetime.fromisoformat(existing["last_seen"])
            seconds_since = (datetime.now(timezone.utc) - last_seen).total_seconds()

            if seconds_since >= self.AGENT_TIMEOUT_SECONDS:
                return candidate

            counter += 1

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
            Agent data dict with status field, or None if not found
        """
        agent_data = self._get_agent_raw(agent_id)
        if agent_data is None:
            return None

        return self._add_status(agent_data)

    def remove_agent(self, agent_id: str) -> bool:
        """Remove an agent from the registry.

        Args:
            agent_id: The agent ID to remove

        Returns:
            True if agent was removed, False if not found
        """
        result = self.redis.hdel(self.AGENTS_KEY, agent_id)
        return result > 0

    def count_online_agents(self) -> int:
        """Count the number of currently online agents.

        Returns:
            Number of agents with status 'online'
        """
        agents = self.list_agents()
        return sum(1 for a in agents if a.get("status") == "online")
