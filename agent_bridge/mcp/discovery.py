"""Tool discovery via MCP tools/list with declarative catalog fallback."""

from __future__ import annotations

from typing import Any

import structlog
import yaml

from agent_bridge.mcp.gateway import PermissionGateway
from agent_bridge.mcp.models import McpServerConfig, ToolSpec, resolve_project_path
from agent_bridge.mcp.transport import McpTransport

logger = structlog.get_logger(__name__)

KNOWLEDGE_PREFIXES = (
    "entry_",
    "block_",
    "space_",
    "file_",
    "smartsheet_",
    "tag_",
    "comment_",
    "udf_",
    "translation_",
)


class ToolDiscovery:
    """Turn an MCP tool listing into namespaced :class:`ToolSpec` objects."""

    def discover(self, transport: McpTransport, config: McpServerConfig) -> list[ToolSpec]:
        """Discover tools; fall back to the declarative catalog on failure."""
        try:
            raw_tools = transport.list_tools()
        except Exception as exc:
            logger.warning(
                "mcp_tools_list_failed",
                server=config.name,
                error=str(exc),
                message="使用声明式 catalog 回退",
            )
            raw_tools = []
        specs = [self._spec_from_raw(item, config) for item in raw_tools if self._valid(item)]
        if not specs:
            logger.info("mcp_catalog_fallback", server=config.name)
            return self._fallback_catalog(config)
        logger.info("mcp_tools_discovered", server=config.name, count=len(specs))
        return specs

    def _valid(self, item: Any) -> bool:
        """Return True for tool mappings carrying a name."""
        return isinstance(item, dict) and bool(item.get("name"))

    def _spec_from_raw(self, item: dict[str, Any], config: McpServerConfig) -> ToolSpec:
        """Build a ToolSpec from a tools/list entry (primary path)."""
        name = str(item["name"])
        qname = f"{config.namespace}__{name}"  # 设计约定：主路径只加一层命名空间
        category = self._category_for(name)
        return ToolSpec(
            name=name,
            qualified_name=qname,
            description=str(item.get("description", "") or ""),
            input_schema=item.get("inputSchema") or item.get("input_schema") or {"type": "object", "properties": {}},
            server=config.name,
            permission=PermissionGateway.classify_name(name, config.write_tools),
            permission_explicit=True,
            category=category,
        )

    def _category_for(self, name: str) -> str | None:
        """Infer a disambiguating category from the tool name."""
        if name.startswith(KNOWLEDGE_PREFIXES):
            return "knowledge"
        if name.startswith("ppt_"):
            return "ai"
        if name.startswith("team_"):
            return "teamspace"
        return None

    def _fallback_catalog(self, config: McpServerConfig) -> list[ToolSpec]:
        """Build ToolSpecs from ``catalog_inline`` or a YAML catalog file."""
        if config.catalog_inline:
            data: dict[str, Any] = {
                "namespace": config.namespace,
                "default_input_schema": {"type": "object", "properties": {}},
                "categories": [{"qualifier": "", "tools": config.catalog_inline}],
            }
        else:
            path = resolve_project_path(config.catalog_path or "config/lexiang_tools.yaml")
            if not path.is_file():
                logger.error("mcp_catalog_missing", server=config.name, path=str(path))
                return []
            try:
                loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError) as exc:
                logger.error("mcp_catalog_invalid", server=config.name, error=str(exc))
                return []
            data = loaded if isinstance(loaded, dict) else {}
        namespace = str(data.get("namespace", config.namespace))
        default_schema = data.get("default_input_schema") or {"type": "object", "properties": {}}
        categories = data.get("categories", [])
        specs: list[ToolSpec] = []
        if isinstance(categories, dict):
            categories = [
                {"key": key, **(value if isinstance(value, dict) else {})} for key, value in categories.items()
            ]
        for category in categories:
            if not isinstance(category, dict):
                continue
            qualifier = str(category.get("qualifier", "") or "")
            description = str(category.get("description", "") or "")
            descriptions = category.get("tool_descriptions") or {}
            schemas = category.get("schemas") or {}
            for entry in category.get("tools", []) or []:
                if isinstance(entry, str):
                    name, explicit = entry, {}
                elif isinstance(entry, dict) and entry.get("name"):
                    name, explicit = str(entry["name"]), entry
                else:
                    continue
                tool_desc = (
                    explicit.get("description")
                    or (descriptions.get(name) if isinstance(descriptions, dict) else None)
                    or f"{description} - {name}"
                )
                schema = (
                    explicit.get("input_schema")
                    or explicit.get("inputSchema")
                    or (schemas.get(name) if isinstance(schemas, dict) else None)
                    or default_schema
                )
                qname = f"{namespace}__{qualifier}__{name}" if qualifier else f"{namespace}__{name}"
                specs.append(
                    ToolSpec(
                        name=name,
                        qualified_name=qname,
                        description=str(tool_desc),
                        input_schema=schema,
                        server=config.name,
                        permission=PermissionGateway.classify_name(name, config.write_tools),
                        permission_explicit=True,
                        category=qualifier or self._category_for(name),
                    )
                )
        return specs
