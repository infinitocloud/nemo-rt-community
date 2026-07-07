"""File-based config store (database-free).

Replaces the old `db` package's config layer with a tiny JSON file on disk.
Single-tenant: there is exactly one config namespace, so the server↔tenant
cascade (system_prompt concat, max_tokens cap) is gone — tenant_id is ignored.

The async method signatures match what web_app.py expects, so call sites are
unchanged (only the init differs).
"""
from __future__ import annotations
import json
import os
from dataclasses import dataclass


@dataclass
class ConfigEntry:
    key: str
    value: str


class ConfigStore:
    """In-memory dict[str, str] persisted to a JSON file atomically."""

    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get("CONFIG_FILE", "data/config.json")
        self._config: dict[str, str] = {}

    # --- Lifecycle ---

    async def initialize(self) -> None:
        """Create the parent dir if missing; load the JSON file if it exists."""
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Keys/values are strings (same contract as the old DB).
            self._config = {str(k): str(v) for k, v in data.items()}
        else:
            self._config = {}

    async def close(self) -> None:
        """No-op (nothing to release for a JSON file)."""
        return None

    # --- Persistence ---

    def _persist(self) -> None:
        """Write the dict to the JSON file atomically (tmp + os.replace)."""
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._config, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # --- Server config ---

    async def get_config(self, key: str) -> str | None:
        return self._config.get(key)

    async def set_config(self, key: str, value: str) -> None:
        self._config[key] = value
        self._persist()

    async def get_all_config(self) -> list[ConfigEntry]:
        return [ConfigEntry(key=k, value=v) for k, v in self._config.items()]

    async def delete_config(self, key: str) -> None:
        if key in self._config:
            del self._config[key]
            self._persist()

    # --- Tenant config (single-tenant: tenant_id ignored, same dict) ---

    async def get_tenant_config(self, tenant_id: str) -> dict[str, str]:
        return dict(self._config)

    async def set_tenant_config(self, tenant_id: str, key: str, value: str) -> None:
        self._config[key] = value
        self._persist()

    async def delete_tenant_config(self, tenant_id: str, key: str) -> None:
        if key in self._config:
            del self._config[key]
            self._persist()

    # --- Effective config (single-tenant: just the full dict) ---

    async def get_effective_config(self, tenant_id: str | None = None) -> dict[str, str]:
        return dict(self._config)
