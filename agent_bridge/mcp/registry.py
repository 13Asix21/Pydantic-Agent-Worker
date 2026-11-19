"""Multi-server MCP lifecycle, namespacing, conflict detection and isolation."""

from __future__ import annotations

import structlog
import yaml

from agent_bridge.config.settings import Settings, config_dir
from agent_bridge.mcp.discovery import ToolDiscovery
from agent_bridge.mcp.models import McpServerConfig, ToolSpec
from agent_bridge.mcp.proxy import McpToolProxy
from agent_bridge.mcp.transport import McpTransport
from agent_bridge.tools.registry import qualify_tool_name

logger = structlog.get_logger(__name__)


class McpManager:
    """Own all MCP servers; one broken server never breaks the others."""

    def __init__(self) -> None:
        """Create an empty manager."""
        self._configs: dict[str, McpServerConfig] = {}
        self._tools_by_server: dict[str, list[ToolSpec]] = {}
        self._transports: dict[str, McpTransport] = {}
        self._unhealthy: set[str] = set()
        self._proxy: McpToolProxy | None = None
        self._settings: Settings | None = None

    def startup(
        self,
        settings: Settings | None = None,
        transports: dict[str, McpTransport] | None = None,
    ) -> None:
        """Load config, connect every server, discover tools, build the proxy.

        Args:
            settings: Settings override (tests use a dry_run-toggled copy).
            transports: Pre-built transports keyed by server name; injected in
                tests and for the PoC mock environment.
        """
        self._settings = settings or Settings()
        overrides = transports or {}
        for config in self._load_configs():
            self._configs[config.name] = config
            if not config.enabled:
                self._unhealthy.add(config.name)
                continue
            try:
                transport = overrides.get(config.name) or McpTransport.create(config)
                transport.connect()
                specs = ToolDiscovery().discover(transport, config)
                self._transports[config.name] = transport
                self._tools_by_server[config.name] = specs
                logger.info(
                    "mcp_server_ready",
                    server=config.name,
                    namespace=config.namespace,
                    tools=len(specs),
                )
            except Exception as exc:
                self._unhealthy.add(config.name)
                logger.error("mcp_server_failed", server=config.name, error=str(exc))
        self._resolve_conflicts()
        specs_index = {spec.qualified_name: spec for spec in self.all_tools()}
        self._proxy = McpToolProxy(self._transports, specs_index, settings=self._settings)

    def _load_configs(self) -> list[McpServerConfig]:
        """Read ``config/mcp_servers.yaml`` into typed configs."""
        path = config_dir() / "mcp_servers.yaml"
        if not path.is_file():
            logger.error("mcp_config_missing", path=str(path))
            return []
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            logger.error("mcp_config_invalid", path=str(path), error=str(exc))
            return []
        assert self._settings is not None
        configs: list[McpServerConfig] = []
        for raw in data.get("servers", []) or []:
            payload = dict(raw)
            payload.setdefault("timeout_s", self._settings.mcp.default_timeout_s)
            payload.setdefault("max_retries", self._settings.mcp.max_retries)
            payload.setdefault("failure_threshold", self._settings.mcp.failure_threshold)
            payload.setdefault("cooldown_s", self._settings.mcp.cooldown_s)
            configs.append(McpServerConfig(**payload))
        return configs

    def _resolve_conflicts(self) -> None:
        """Escalate duplicate qualified names to ``<ns>__<category>__<tool>``."""
        seen: dict[str, str] = {}
        for server, specs in self._tools_by_server.items():
            for spec in specs:
                original = spec.qualified_name
                if original in seen and seen[original] != server:
                    upgraded = qualify_tool_name(original, spec.category or server)
                    while upgraded in seen:
                        upgraded = f"{upgraded}_2"
                    spec.qualified_name = upgraded
                    logger.warning(
                        "tool_conflict_upgraded",
                        original=original,
                        upgraded=upgraded,
                        server=server,
                    )
                seen[spec.qualified_name] = server

    def all_tools(self) -> list[ToolSpec]:
        """Return all discovered tools across healthy servers."""
        return [spec for specs in self._tools_by_server.values() for spec in specs]

    def tools_for(self, server: str) -> list[ToolSpec]:
        """Return tools for one server."""
        return list(self._tools_by_server.get(server, []))

    def get_proxy(self) -> McpToolProxy:
        """Return the shared generic proxy."""
        if self._proxy is None:
            raise RuntimeError("McpManager 尚未 startup")
        return self._proxy

    def health(self, server: str) -> bool:
        """Whether a server connected successfully and is not disabled."""
        return server in self._transports and server not in self._unhealthy

    def unhealthy_servers(self) -> list[str]:
        """Return names of servers that failed to connect."""
        return sorted(self._unhealthy)
