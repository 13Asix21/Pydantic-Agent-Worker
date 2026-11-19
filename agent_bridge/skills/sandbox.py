"""Subprocess sandbox for skill scripts: locked cwd, timeouts, whitelists, audit."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import structlog

from agent_bridge.config.settings import PROJECT_ROOT, SandboxSettings, Settings
from agent_bridge.mcp.models import ToolCallResult
from agent_bridge.skills.models import SkillDoc, SkillManifest

logger = structlog.get_logger(__name__)


class SandboxError(ValueError):
    """Sandbox policy violation with a stable error code."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


class ScriptSandbox:
    """Execute skill scripts in a child process with strict path/policy checks."""

    def __init__(self, config: SandboxSettings | None = None) -> None:
        """Create a sandbox from settings (tests may inject a policy)."""
        self.config = config or Settings().sandbox

    def run(
        self,
        skill: SkillDoc,
        script: str,
        args: list[str] | None = None,
        timeout: int | None = None,
    ) -> ToolCallResult:
        """Run a script and return a unified :class:`ToolCallResult`.

        Error codes: ``4001`` path traversal, ``4002`` timeout,
        ``4003`` interpreter/suffix not allowed, ``5002`` execution failure.
        """
        started = time.perf_counter()
        args = args or []
        effective_timeout = min(timeout or self.config.default_timeout_s, self.config.max_timeout_s)
        data: dict[str, object] = {}
        error_code = "0"
        message = "ok"
        ok = False
        path: Path | None = None
        root: Path = Path(".")
        try:
            path = self._resolve_safe_path(skill, script)
            root = skill.manifest.source_path.parent.resolve()
            command = self._build_command(path, args)
            completed = subprocess.run(
                command,
                cwd=str(root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=effective_timeout,
                check=False,
                env=os.environ.copy(),
            )
            data = {
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "returncode": completed.returncode,
                "script": script,
            }
            ok = completed.returncode == 0
            error_code = "0" if ok else "5002"
            message = "ok" if ok else f"脚本退出码 {completed.returncode}"
        except SandboxError as exc:
            error_code, message = exc.error_code, exc.message
        except subprocess.TimeoutExpired as exc:
            error_code = "4002"
            message = f"脚本执行超时（{effective_timeout}s）"
            data = {"stdout": (exc.stdout or ""), "stderr": (exc.stderr or "")}
        except OSError as exc:
            error_code = "4003"
            message = f"解释器或脚本不可执行: {exc}"
        except Exception as exc:  # pragma: no cover - defensive catch-all
            error_code = "5002"
            message = f"沙箱内部错误: {exc}"
        latency = int((time.perf_counter() - started) * 1000)
        result = ToolCallResult(
            ok=ok,
            data=data,
            error_code=error_code,
            message=message,
            latency_ms=latency,
            dry_run=False,
        )
        self._audit(result, skill.manifest, script, root)
        return result

    def _resolve_safe_path(self, skill: SkillDoc, script: str) -> Path:
        """Resolve the script, enforcing relative paths and suffix whitelist."""
        root = skill.manifest.source_path.parent.resolve()
        candidate = Path(script)
        if candidate.is_absolute():
            raise SandboxError("4001", f"禁止绝对路径: {script}")
        target = (root / candidate).resolve()
        if target != root and root not in target.parents:
            raise SandboxError("4001", f"路径穿越被拒绝: {script}")
        if target.suffix.lower() not in self.config.allowed_suffixes:
            raise SandboxError("4003", f"脚本后缀不在白名单: {target.suffix}")
        if not target.is_file():
            raise SandboxError("4001", f"脚本不存在: {script}")
        return target

    def _build_command(self, path: Path, args: list[str]) -> list[str]:
        """Build the child-process command using the whitelisted interpreter."""
        interpreter = Path(sys.executable).name.lower()
        allowed = {item.lower() for item in self.config.allowed_interpreters}
        if interpreter not in allowed and "python" not in allowed and "python3" not in allowed:
            raise SandboxError("4003", f"解释器不在白名单: {interpreter}")
        return [sys.executable, str(path), *[str(arg) for arg in args]]

    def _audit(self, result: ToolCallResult, manifest: SkillManifest, script: str, root: Path) -> None:
        """Write one audit record; never log raw arguments (may contain secrets)."""
        record = {
            "trace_id": result.trace_id,
            "skill": manifest.name,
            "script": script,
            "cwd": str(root),
            "code": result.error_code,
            "latency_ms": result.latency_ms,
            "dry_run": False,
        }
        logger.info("sandbox_exec", **record)
        audit_path = PROJECT_ROOT / self.config.audit_log
        try:
            audit_path.parent.mkdir(parents=True, exist_ok=True)
            with audit_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:  # pragma: no cover - logging must never break execution
            logger.warning("sandbox_audit_failed", error=str(exc))
