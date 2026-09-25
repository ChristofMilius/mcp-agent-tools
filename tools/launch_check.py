from __future__ import annotations

import argparse
import configparser
import copy
import difflib
import json
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


def _redact_text(value: str) -> str:
    home = str(Path.home())
    for candidate in (home, home.replace("\\", "/")):
        if candidate:
            value = value.replace(candidate, "~")
    return value


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact_value(item) for key, item in value.items()}
    return value


@dataclass
class Finding:
    severity: str
    code: str
    message: str
    app: str | None = None
    tool: str | None = None
    entry_index: int | None = None
    suggestion: Any = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "severity": self.severity,
            "code": self.code,
            "message": _redact_text(self.message),
        }
        if self.app is not None:
            result["app"] = self.app
        if self.tool is not None:
            result["tool"] = self.tool
        if self.entry_index is not None:
            result["entry_index"] = self.entry_index
        if self.suggestion is not None:
            result["suggestion"] = _redact_value(self.suggestion)
        return result


@dataclass
class CheckReport:
    findings: list[Finding] = field(default_factory=list)
    plans: list["ConfigPlan"] = field(default_factory=list)

    @property
    def fail_count(self) -> int:
        return sum(finding.severity == "FAIL" for finding in self.findings)

    @property
    def warning_count(self) -> int:
        return sum(finding.severity == "WARN" for finding in self.findings)

    @property
    def info_count(self) -> int:
        return sum(finding.severity == "INFO" for finding in self.findings)

    def add(self, finding: Finding, logger: logging.Logger | None = None) -> None:
        self.findings.append(finding)
        if logger is not None:
            level = {
                "FAIL": logging.ERROR,
                "WARN": logging.WARNING,
                "INFO": logging.INFO,
            }.get(finding.severity, logging.INFO)
            logger.log(level, format_finding(finding))

    def to_dict(self, include_diff: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": "FAIL" if self.fail_count else "OK",
            "fail_count": self.fail_count,
            "warning_count": self.warning_count,
            "info_count": self.info_count,
            "findings": [finding.to_dict() for finding in self.findings],
            "apps": [plan.to_dict(include_diff=include_diff) for plan in self.plans],
        }
        if include_diff:
            result["diff"] = render_diffs(self.plans)
        return result


@dataclass
class ConfigPlan:
    app: str
    path: Path
    original_text: str
    proposed_text: str = ""
    changes: list[str] = field(default_factory=list)

    def to_dict(self, include_diff: bool = False) -> dict[str, Any]:
        logical_path = f"{self.app}/.internal/ng-mcp.json"
        result: dict[str, Any] = {
            "app": self.app,
            "path": logical_path,
            "changes": list(self.changes),
        }
        if include_diff:
            result["diff"] = self.diff()
        return result

    def diff(self) -> str:
        if not self.changes or not self.proposed_text:
            return ""
        old_text = redact_json_text(self.original_text)
        new_text = redact_json_text(self.proposed_text)
        logical_path = f"{self.app}/.internal/ng-mcp.json"
        return "".join(
            difflib.unified_diff(
                old_text.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=logical_path,
                tofile=f"{logical_path} (proposed)",
            )
        )


@dataclass
class ToolSpec:
    directory: Path
    relative_path: str
    project_name: str
    script_name: str
    target_module: str
    package: str
    has_main: bool
    alias_keys: tuple[str, ...]
    metadata_error: str | None = None

    @property
    def short_name(self) -> str:
        for value in (self.project_name, self.script_name, self.relative_path):
            key = _name_key(value)
            if key.startswith("mcpagent") and len(key) > len("mcpagent"):
                return _display_short_name(value)
        return self.script_name

    @property
    def module_args(self) -> list[str]:
        return ["python", "-m", self.package]

    @property
    def script_args(self) -> list[str]:
        return [self.script_name]

    def prefix_for(self, kind: str) -> list[str]:
        launch_args = self.module_args if kind == "module" else self.script_args
        return ["--project", str(self.directory), "run", *launch_args]

    def accepts(self, name: str) -> bool:
        return _name_key(name) in self.alias_keys


@dataclass
class HelpResult:
    ok: bool
    output: str = ""
    error: str = ""


@dataclass
class NearMatch:
    kind: str
    replacement_args: list[str]
    distance: int
    usable: bool


@dataclass
class LaunchAssessment:
    valid: bool = False
    kind: str | None = None
    trailing: tuple[str, ...] = ()
    subcommand: str | None = None
    code: str | None = None
    message: str | None = None
    suggestion: Any = None
    repair_command: str | None = None
    repair_args: list[str] | None = None
    path_divergent: bool = False
    form_divergent: bool = False
    help_result: HelpResult | None = None


HelpRunner = Callable[[ToolSpec, Sequence[str], str | None], HelpResult]


def _name_key(value: Any) -> str:
    text = str(value).casefold()
    return "".join(character for character in text if character.isalnum())


def _display_short_name(value: str) -> str:
    for prefix in ("mcp-agent-", "mcp_agent_"):
        if value.casefold().startswith(prefix.casefold()):
            return value[len(prefix) :]
    return value


def _alias_keys(values: Iterable[str]) -> tuple[str, ...]:
    result: set[str] = set()
    for value in values:
        key = _name_key(value)
        if not key:
            continue
        result.add(key)
        if key.startswith("mcpagent") and len(key) > len("mcpagent"):
            result.add(key[len("mcpagent") :])
    return tuple(sorted(result))


def _submodule_paths(gitmodules: Path) -> tuple[list[str], list[str]]:
    parser = configparser.RawConfigParser()
    errors: list[str] = []
    try:
        with gitmodules.open("r", encoding="utf-8") as stream:
            parser.read_file(stream)
    except OSError as exc:
        return [], [f"cannot read .gitmodules: {exc}"]
    except configparser.Error as exc:
        return [], [f"malformed .gitmodules: {exc}"]

    paths: list[str] = []
    seen: set[str] = set()
    for section in parser.sections():
        if not section.casefold().startswith("submodule"):
            continue
        try:
            raw_path = parser.get(section, "path")
        except (configparser.Error, KeyError):
            errors.append(f"{section}: missing path")
            continue
        raw_path = raw_path.strip()
        if len(raw_path) >= 2 and raw_path[0] == raw_path[-1] and raw_path[0] in "\"'":
            raw_path = raw_path[1:-1]
        path = Path(raw_path)
        if path.is_absolute() or ".." in path.parts:
            errors.append(f"{section}: unsafe path {raw_path!r}")
            continue
        normalized = path.as_posix()
        if normalized in seen:
            errors.append(f"{section}: duplicate path {raw_path!r}")
            continue
        seen.add(normalized)
        paths.append(normalized)
    if not paths and not errors:
        errors.append("no submodule paths found")
    return paths, errors


def _load_tool_spec(tool_dir: Path, relative_path: str) -> tuple[ToolSpec | None, list[Finding]]:
    findings: list[Finding] = []
    pyproject_path = tool_dir / "pyproject.toml"
    try:
        with pyproject_path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return None, [
            Finding(
                "FAIL",
                "inventory-pyproject",
                f"cannot read {relative_path}/pyproject.toml: {exc}",
                tool=relative_path,
            )
        ]

    project = data.get("project")
    if not isinstance(project, dict):
        message = f"{relative_path}/pyproject.toml has no [project] table"
        findings.append(Finding("FAIL", "inventory-project", message, tool=relative_path))
        return None, findings

    scripts = project.get("scripts")
    if not isinstance(scripts, dict) or len(scripts) != 1:
        message = f"{relative_path} must define exactly one [project.scripts] entry"
        findings.append(Finding("FAIL", "inventory-scripts", message, tool=relative_path))
        return None, findings

    script_name, target = next(iter(scripts.items()))
    if not isinstance(script_name, str) or not script_name:
        message = f"{relative_path} has an invalid console script name"
        findings.append(Finding("FAIL", "inventory-script-name", message, tool=relative_path))
        return None, findings
    if not isinstance(target, str) or ":" not in target:
        message = f"{relative_path} has an invalid console script target for {script_name}"
        findings.append(Finding("FAIL", "inventory-script-target", message, tool=relative_path))
        return None, findings

    target_module = target.split(":", 1)[0]
    if not target_module:
        message = f"{relative_path} has an empty target module for {script_name}"
        findings.append(Finding("FAIL", "inventory-script-target", message, tool=relative_path))
        return None, findings
    package = target_module.split(".", 1)[0]
    project_name = project.get("name")
    if not isinstance(project_name, str) or not project_name:
        project_name = script_name
    main_path = tool_dir / "src" / package / "__main__.py"
    aliases = _alias_keys((project_name, script_name, relative_path, Path(relative_path).name))
    return (
        ToolSpec(
            directory=tool_dir,
            relative_path=relative_path,
            project_name=project_name,
            script_name=script_name,
            target_module=target_module,
            package=package,
            has_main=main_path.is_file(),
            alias_keys=aliases,
        ),
        findings,
    )


def load_inventory(repo_root: Path) -> tuple[list[ToolSpec], list[Finding]]:
    relative_paths, errors = _submodule_paths(repo_root / ".gitmodules")
    findings = [
        Finding("FAIL", "inventory-gitmodules", message) for message in errors
    ]
    tools: list[ToolSpec] = []
    seen: set[str] = set()
    for relative_path in relative_paths:
        if relative_path in seen:
            continue
        seen.add(relative_path)
        tool_dir = (repo_root / relative_path).resolve()
        if not tool_dir.is_dir():
            findings.append(
                Finding(
                    "FAIL",
                    "inventory-missing-tool",
                    f"submodule directory does not exist: {relative_path}",
                    tool=relative_path,
                )
            )
            continue
        spec, spec_findings = _load_tool_spec(tool_dir=tool_dir, relative_path=relative_path)
        findings.extend(spec_findings)
        if spec is not None:
            tools.append(spec)
    return tools, findings


def _path_value(value: Any) -> Path | None:
    if not isinstance(value, (str, os.PathLike)) or not os.fspath(value):
        return None
    try:
        path = Path(os.fspath(value)).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        return path
    except (OSError, ValueError):
        return None


def _path_key(value: Any) -> str | None:
    path = _path_value(value)
    if path is None:
        return None
    try:
        return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))
    except (OSError, ValueError):
        return None


def _same_path(left: Any, right: Any) -> bool:
    left_key = _path_key(left)
    right_key = _path_key(right)
    return left_key is not None and left_key == right_key


def _existing_directory(value: Any) -> bool:
    path = _path_value(value)
    return path is not None and path.is_dir()


def _is_uv_command(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    base = value.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    return base in {"uv", "uv.exe"}


def _script_file(spec: ToolSpec) -> Path | None:
    directories = (spec.directory / ".venv" / "Scripts", spec.directory / ".venv" / "bin")
    names = (
        spec.script_name,
        f"{spec.script_name}.exe",
        f"{spec.script_name}.cmd",
        f"{spec.script_name}.bat",
        f"{spec.script_name}.ps1",
    )
    for directory in directories:
        for name in names:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def _bounded_edit_distance(left: str, right: str, limit: int) -> int:
    if abs(len(left) - len(right)) > limit:
        return limit + 1
    previous = list(range(len(right) + 1))
    for left_index, left_character in enumerate(left, start=1):
        current = [left_index]
        row_minimum = left_index
        for right_index, right_character in enumerate(right, start=1):
            value = min(
                current[right_index - 1] + 1,
                previous[right_index] + 1,
                previous[right_index - 1] + (left_character != right_character),
            )
            current.append(value)
            row_minimum = min(row_minimum, value)
        if row_minimum > limit:
            return limit + 1
        previous = current
    return previous[-1]


def _token_edit_distance(left: Sequence[str], right: Sequence[str], limit: int = 3) -> int:
    if abs(len(left) - len(right)) > limit:
        return limit + 1
    previous = list(range(len(right) + 1))
    for left_index, left_token in enumerate(left, start=1):
        current = [left_index]
        row_minimum = left_index
        for right_index, right_token in enumerate(right, start=1):
            value = min(
                current[right_index - 1] + 1,
                previous[right_index] + 1,
                previous[right_index - 1] + (left_token != right_token),
            )
            current.append(value)
            row_minimum = min(row_minimum, value)
        if row_minimum > limit:
            return limit + 1
        previous = current
    return previous[-1]


def _near_command(value: str) -> str | None:
    if _bounded_edit_distance(value.casefold(), "uv", 2) <= 2:
        return "uv"
    return None


def _near_launch(spec: ToolSpec, args: Sequence[str]) -> NearMatch | None:
    if not args:
        return None
    candidates: list[NearMatch] = []
    for kind in ("module", "script"):
        prefix = spec.prefix_for(kind)
        candidate = list(prefix)
        if len(args) >= 2 and _same_path(args[1], spec.directory):
            candidate[1] = args[1]
        if len(args) >= len(prefix):
            candidate.extend(args[len(prefix) :])
        distance = _token_edit_distance(args, candidate, 2)
        if distance > 2 or distance >= len(candidate):
            continue
        differences = [
            index
            for index in range(max(len(args), len(candidate)))
            if (args[index] if index < len(args) else None)
            != (candidate[index] if index < len(candidate) else None)
        ]
        if not any(index >= 3 for index in differences):
            continue
        if all(index in {1} for index in differences):
            continue
        usable = spec.has_main if kind == "module" else _script_file(spec) is not None
        candidates.append(NearMatch(kind, candidate, distance, usable))
    if not candidates:
        return None
    return min(candidates, key=lambda candidate: (candidate.distance, candidate.kind))


def _default_help_runner(
    spec: ToolSpec,
    launch_args: Sequence[str],
    subcommand: str | None,
) -> HelpResult:
    executable = shutil.which("uv") or shutil.which("uv.exe")
    if executable is None:
        return HelpResult(False, error="uv is not available on PATH")
    command = [executable, "--project", str(spec.directory), "run", *launch_args]
    if subcommand is not None:
        command.append(subcommand)
    command.append("--help")
    try:
        completed = subprocess.run(
            command,
            cwd=str(spec.directory),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return HelpResult(False, error=f"could not run help: {type(exc).__name__}")
    output = (completed.stdout or "") + (completed.stderr or "")
    return HelpResult(completed.returncode == 0, output=output)


def _help_contains(output: str, subcommand: str) -> bool:
    pattern = rf"(?<![A-Za-z0-9_-]){re.escape(subcommand)}(?![A-Za-z0-9_-])"
    return re.search(pattern, output) is not None


def _launch_issue(
    code: str,
    message: str,
    suggestion: Any = None,
    repair_command: str | None = None,
    repair_args: list[str] | None = None,
) -> LaunchAssessment:
    return LaunchAssessment(
        valid=False,
        code=code,
        message=message,
        suggestion=suggestion,
        repair_command=repair_command,
        repair_args=repair_args,
    )


def _near_issue(spec: ToolSpec, args: Sequence[str], fallback_message: str) -> LaunchAssessment:
    near = _near_launch(spec, args)
    if near is None:
        return _launch_issue("invalid-launch", fallback_message)
    replacement = {
        "command": "uv",
        "args": near.replacement_args,
    }
    message = (
        f"configured launch is not valid but is a near-match for the {near.kind} form "
        f"(edit distance {near.distance}); suggested replacement is {json.dumps(replacement, ensure_ascii=False)}"
    )
    return _launch_issue(
        "typo",
        message,
        suggestion=replacement,
        repair_command="uv" if near.usable else None,
        repair_args=near.replacement_args if near.usable else None,
    )


def assess_launch(
    spec: ToolSpec,
    command: Any,
    args: Any,
    help_runner: HelpRunner,
    help_cache: dict[tuple[Any, ...], HelpResult],
) -> LaunchAssessment:
    if not isinstance(command, str) or not command:
        return _launch_issue("bad-command", "connection.command must be a non-empty string")
    if not _is_uv_command(command):
        replacement = _near_command(command)
        if replacement is not None:
            suggestion = {"command": replacement, "args": args if isinstance(args, list) else []}
            return _launch_issue(
                "typo",
                f"configured command is a near-match for uv; suggested replacement is {json.dumps(suggestion, ensure_ascii=False)}",
                suggestion=suggestion,
                repair_command=replacement if isinstance(args, list) else None,
            )
        return _launch_issue("bad-command", f"configured command must be uv, got {command!r}")
    if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
        return _launch_issue("invalid-launch", "connection.args must be a list of strings")
    args_tuple = tuple(args)
    if len(args) < 4 or args[0] != "--project" or args[2] != "run":
        return _near_issue(
            spec,
            args_tuple,
            "launch args must start with uv --project <tool-dir> run",
        )
    if not _same_path(args[1], spec.directory):
        return _launch_issue(
            "bad-project-dir",
            "project directory does not match the derived coordinator submodule directory",
        )

    rest = args[3:]
    if rest[:3] == ["python", "-m", spec.package]:
        kind = "module"
        head_length = 3
    elif rest and rest[0] == spec.script_name:
        kind = "script"
        head_length = 1
    else:
        return _near_issue(
            spec,
            args_tuple,
            f"launch target must be python -m {spec.package} or {spec.script_name}",
        )

    trailing = tuple(rest[head_length:])
    subcommand = next((token for token in trailing if not token.startswith("-")), None)
    result = LaunchAssessment(
        valid=True,
        kind=kind,
        trailing=trailing,
        subcommand=subcommand,
        path_divergent=args[1] != str(spec.directory),
        form_divergent=kind == "script" and spec.has_main,
    )
    if kind == "module" and not spec.has_main:
        return LaunchAssessment(
            valid=False,
            code="module-form-unavailable",
            message=f"module form is unavailable because {spec.relative_path}/src/{spec.package}/__main__.py does not exist",
        )
    if kind == "script" and _script_file(spec) is None:
        cache_key = (str(spec.directory), kind, subcommand)
        if cache_key not in help_cache:
            help_cache[cache_key] = help_runner(spec, spec.script_args, subcommand)
        result.help_result = help_cache[cache_key]
        if not result.help_result.ok:
            return LaunchAssessment(
                valid=False,
                code="script-form-unavailable",
                message=f"console script {spec.script_name!r} is not installed and uv could not resolve it",
                help_result=result.help_result,
            )
    if subcommand is not None:
        launch_args = spec.module_args if kind == "module" else spec.script_args
        cache_key = (str(spec.directory), kind, subcommand)
        if cache_key not in help_cache:
            help_cache[cache_key] = help_runner(spec, launch_args, subcommand)
        result.help_result = help_cache[cache_key]
        if not result.help_result.ok:
            result.valid = False
            result.code = "subcommand-unverified"
            result.message = f"could not obtain --help for subcommand {subcommand!r}"
            return result
        if not _help_contains(result.help_result.output, subcommand):
            result.valid = False
            result.code = "unknown-subcommand"
            result.message = f"subcommand {subcommand!r} does not appear in the server's --help"
    return result


def _redact_json(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).casefold() == "env" and isinstance(item, dict):
                result[key] = {str(env_key): "<redacted>" for env_key in item}
            else:
                result[key] = _redact_json(item)
        return result
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def redact_json_text(text: str) -> str:
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
    return json.dumps(_redact_json(value), indent=2, ensure_ascii=False) + "\n"


def _serialize_json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def _format_args(args: Sequence[str]) -> str:
    return json.dumps(list(args), ensure_ascii=False)


def _finding_location(finding: Finding) -> str:
    location = f"[{finding.app}]" if finding.app else "[coordinator]"
    if finding.entry_index is not None:
        location += f" entry[{finding.entry_index}]"
    if finding.tool:
        location += f" {finding.tool}"
    return location


def format_finding(finding: Finding) -> str:
    text = f"{finding.severity} {_finding_location(finding)} {finding.code}: {_redact_text(finding.message)}"
    if finding.suggestion is not None:
        text += f" suggestion={json.dumps(_redact_value(finding.suggestion), ensure_ascii=False)}"
    return text


def _add_entry_structure_findings(
    app: str,
    index: int,
    entry: Any,
    report: CheckReport,
    logger: logging.Logger | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not isinstance(entry, dict):
        report.add(
            Finding("FAIL", "malformed-entry", "server entries must be objects", app=app, entry_index=index),
            logger,
        )
        return None, None
    if not isinstance(entry.get("id"), str) or not entry.get("id"):
        report.add(
            Finding("FAIL", "invalid-id", "server id must be a non-empty string and is preserved verbatim", app=app, entry_index=index),
            logger,
        )
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        report.add(
            Finding("FAIL", "unknown-name", "server name must be a non-empty string", app=app, entry_index=index),
            logger,
        )
    enabled = entry.get("enabled")
    if not isinstance(enabled, bool):
        report.add(
            Finding("FAIL", "invalid-enabled", "server enabled must be a boolean", app=app, entry_index=index),
            logger,
        )
    elif not enabled:
        report.add(
            Finding("FAIL", "disabled-entry", "server is disabled", app=app, entry_index=index),
            logger,
        )
    connection = entry.get("connection")
    if not isinstance(connection, dict):
        report.add(
            Finding("FAIL", "malformed-connection", "server connection must be an object", app=app, entry_index=index),
            logger,
        )
        return entry, None
    if connection.get("type") != "stdio":
        report.add(
            Finding(
                "FAIL",
                "non-stdio",
                f"connection.type must be stdio, got {connection.get('type')!r}",
                app=app,
                entry_index=index,
            ),
            logger,
        )
    args = connection.get("args")
    if "env" in connection and not isinstance(connection["env"], dict):
        report.add(
            Finding("FAIL", "invalid-env", "connection.env must be an object", app=app, entry_index=index),
            logger,
        )
    cwd = connection.get("cwd")
    if cwd is not None and not _existing_directory(cwd):
        report.add(
            Finding("FAIL", "missing-cwd", "connection.cwd does not exist or is not a directory", app=app, entry_index=index),
            logger,
        )
    command = connection.get("command")
    if not isinstance(command, str) or not command:
        report.add(
            Finding("FAIL", "bad-command", "connection.command must be a non-empty string", app=app, entry_index=index),
            logger,
        )
    if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
        report.add(
            Finding("FAIL", "invalid-launch", "connection.args must be a list of strings", app=app, entry_index=index),
            logger,
        )
    return entry, connection


def _tool_lookup(name: str, tools: Sequence[ToolSpec]) -> tuple[int | None, str | None]:
    matches = [index for index, spec in enumerate(tools) if spec.accepts(name)]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, "ambiguous"
    return None, None


def _check_app(
    app: str,
    config_path: Path,
    tools: Sequence[ToolSpec],
    report: CheckReport,
    logger: logging.Logger | None,
    help_runner: HelpRunner,
    help_cache: dict[tuple[Any, ...], HelpResult],
) -> ConfigPlan:
    try:
        original_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        report.add(
            Finding("FAIL", "config-read", f"cannot read config: {type(exc).__name__}", app=app),
            logger,
        )
        return ConfigPlan(app=app, path=config_path, original_text="")
    try:
        data = json.loads(original_text)
    except json.JSONDecodeError as exc:
        report.add(
            Finding(
                "FAIL",
                "malformed-json",
                f"malformed JSON at line {exc.lineno}, column {exc.colno}",
                app=app,
            ),
            logger,
        )
        return ConfigPlan(app=app, path=config_path, original_text=original_text)
    if not isinstance(data, dict):
        report.add(
            Finding("FAIL", "malformed-config", "top-level JSON value must be an object", app=app),
            logger,
        )
        return ConfigPlan(app=app, path=config_path, original_text=original_text)
    if "mcpServers" in data:
        server_key = "mcpServers"
    elif "servers" in data:
        server_key = "servers"
    else:
        report.add(
            Finding("FAIL", "malformed-config", "top-level mcpServers or servers must be present", app=app),
            logger,
        )
        return ConfigPlan(app=app, path=config_path, original_text=original_text)
    entries = data.get(server_key)
    if not isinstance(entries, list):
        report.add(
            Finding("FAIL", "malformed-config", f"top-level {server_key} must be a list", app=app),
            logger,
        )
        return ConfigPlan(app=app, path=config_path, original_text=original_text)

    proposed = copy.deepcopy(data)
    proposed_entries = proposed.get(server_key)
    if not isinstance(proposed_entries, list):
        proposed_entries = []
    changes: list[str] = []
    name_counts: dict[str, int] = {}
    tool_counts: dict[int, int] = {}
    matched_tools: set[int] = set()
    for index, entry in enumerate(entries):
        validated_entry, connection = _add_entry_structure_findings(app, index, entry, report, logger)
        if validated_entry is None:
            continue
        name = validated_entry.get("name")
        if isinstance(name, str) and name.strip():
            name_counts[_name_key(name)] = name_counts.get(_name_key(name), 0) + 1
        tool_index = None
        if isinstance(name, str) and name.strip():
            tool_index, lookup_error = _tool_lookup(name, tools)
            if lookup_error == "ambiguous":
                report.add(
                    Finding("FAIL", "ambiguous-name", f"server name {name!r} matches multiple coordinator tools", app=app, entry_index=index),
                    logger,
                )
            elif tool_index is None:
                report.add(
                    Finding("FAIL", "unknown-name", f"server name {name!r} is not a coordinator tool", app=app, entry_index=index),
                    logger,
                )
        if tool_index is not None:
            tool_counts[tool_index] = tool_counts.get(tool_index, 0) + 1
            matched_tools.add(tool_index)
            if connection is not None:
                spec = tools[tool_index]
                assessment = assess_launch(
                    spec,
                    connection.get("command"),
                    connection.get("args"),
                    help_runner,
                    help_cache,
                )
                if assessment.suggestion is not None and assessment.code == "typo":
                    report.add(
                        Finding(
                            "FAIL",
                            "typo",
                            assessment.message or "launch is a near-match typo",
                            app=app,
                            tool=spec.short_name,
                            entry_index=index,
                            suggestion=assessment.suggestion,
                        ),
                        logger,
                    )
                elif not assessment.valid:
                    report.add(
                        Finding(
                            "FAIL",
                            assessment.code or "invalid-launch",
                            assessment.message or "launch configuration is invalid",
                            app=app,
                            tool=spec.short_name,
                            entry_index=index,
                        ),
                        logger,
                    )
                else:
                    if assessment.path_divergent:
                        report.add(
                            Finding(
                                "INFO",
                                "path-style-divergence",
                                "project path uses a different but equivalent spelling; retaining the configured value",
                                app=app,
                                tool=spec.short_name,
                                entry_index=index,
                            ),
                            logger,
                        )
                    if assessment.form_divergent:
                        report.add(
                            Finding(
                                "INFO",
                                "alternate-launch-form",
                                "script form is valid and retained even though the module form is also available",
                                app=app,
                                tool=spec.short_name,
                                entry_index=index,
                            ),
                            logger,
                        )
                if assessment.repair_args is not None and assessment.repair_command is not None:
                    proposed_connection = proposed_entries[index].get("connection")
                    if isinstance(proposed_connection, dict):
                        old_args = proposed_connection.get("args")
                        old_command = proposed_connection.get("command")
                        if old_args != assessment.repair_args:
                            proposed_connection["args"] = list(assessment.repair_args)
                            changes.append(f"entry[{index}] launch args")
                        if old_command != assessment.repair_command:
                            proposed_connection["command"] = assessment.repair_command
                            changes.append(f"entry[{index}] command")
    for key, count in name_counts.items():
        if count > 1:
            report.add(
                Finding("FAIL", "duplicate-name", f"server name {key!r} appears {count} times", app=app),
                logger,
            )
    for tool_index, count in tool_counts.items():
        if count > 1:
            report.add(
                Finding(
                    "FAIL",
                    "duplicate-tool",
                    f"coordinator tool {tools[tool_index].short_name!r} appears {count} times",
                    app=app,
                ),
                logger,
            )
    for tool_index, spec in enumerate(tools):
        if tool_index not in matched_tools:
            report.add(
                Finding(
                    "INFO",
                    "tool-not-configured",
                    f"{spec.short_name} has no server entry in this app; this is informational",
                    app=app,
                    tool=spec.short_name,
                ),
                logger,
            )
    proposed_text = original_text
    if changes and proposed_entries:
        proposed_text = _serialize_json(proposed)
    return ConfigPlan(
        app=app,
        path=config_path,
        original_text=original_text,
        proposed_text=proposed_text,
        changes=changes,
    )


def _atomic_write(path: Path, text: str) -> bool:
    temporary_path: str | None = None
    try:
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        return True
    except (OSError, ValueError):
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
        return False


def check_repository(
    repo_root: Path,
    apps_root: Path,
    app_filter: str | None = None,
    apply: bool = False,
    help_runner: HelpRunner | None = None,
    logger: logging.Logger | None = None,
) -> CheckReport:
    report = CheckReport()
    tools, inventory_findings = load_inventory(repo_root)
    for finding in inventory_findings:
        report.add(finding, logger)
    effective_help_runner = help_runner or _default_help_runner
    help_cache: dict[tuple[Any, ...], HelpResult] = {}

    if app_filter is not None:
        if Path(app_filter).name != app_filter or app_filter in {"", ".", ".."}:
            report.add(Finding("FAIL", "unknown-app", f"invalid app name: {app_filter!r}"), logger)
            return report
        app_dirs = [apps_root / app_filter]
        if not app_dirs[0].is_dir():
            report.add(Finding("FAIL", "unknown-app", f"LM Studio app directory does not exist: {app_filter}"), logger)
            return report
    else:
        try:
            app_dirs = sorted(
                (path for path in apps_root.iterdir() if path.is_dir()),
                key=lambda path: path.name.casefold(),
            )
        except OSError as exc:
            if not apps_root.exists():
                report.add(Finding("INFO", "no-lm-studio-apps", "LM Studio apps directory does not exist"), logger)
                return report
            report.add(Finding("FAIL", "apps-read", f"cannot read LM Studio apps directory: {type(exc).__name__}"), logger)
            return report
    if not app_dirs:
        report.add(Finding("INFO", "no-lm-studio-apps", "no LM Studio app directories found"), logger)
        return report

    for app_dir in app_dirs:
        app = app_dir.name
        config_path = app_dir / ".internal" / "ng-mcp.json"
        if not config_path.is_file():
            report.add(
                Finding("INFO", "app-config-missing", "app has no .internal/ng-mcp.json", app=app),
                logger,
            )
            continue
        plan = _check_app(
            app,
            config_path,
            tools,
            report,
            logger,
            effective_help_runner,
            help_cache,
        )
        report.plans.append(plan)

    if apply:
        write_failures: list[Finding] = []
        applied = False
        for plan in report.plans:
            if not plan.changes:
                continue
            if _atomic_write(plan.path, plan.proposed_text):
                applied = True
                if logger is not None:
                    logger.info("applied changes to %s: %s", plan.app, ", ".join(plan.changes))
            else:
                write_failures.append(
                    Finding("FAIL", "config-write", "atomic config write failed", app=plan.app)
                )
        if applied:
            refreshed = check_repository(
                repo_root=repo_root,
                apps_root=apps_root,
                app_filter=app_filter,
                help_runner=effective_help_runner,
                logger=logger,
            )
            for finding in write_failures:
                refreshed.add(finding, logger)
            return refreshed
        for finding in write_failures:
            report.add(finding, logger)
    return report


def render_diffs(plans: Sequence[ConfigPlan]) -> str:
    return "\n".join(plan.diff() for plan in plans if plan.diff())


def _configure_logger(repo_root: Path, log_file: str | None) -> logging.Logger | None:
    if not log_file:
        return None
    logger = logging.getLogger("launch_check")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    path = Path(log_file).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    except OSError:
        return None
    return logger


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check LM Studio MCP launch configurations against the coordinator inventory")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--diff", action="store_true", help="show the unified diff that --apply would write")
    mode.add_argument("--apply", action="store_true", help="apply unambiguous launch-prefix repairs atomically")
    parser.add_argument("--app", help="check one LM Studio app directory")
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit one machine-readable JSON object")
    parser.add_argument("--log-file", help="write rotating logs to this path; relative paths are under the repository")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    apps_root = Path.home() / ".lmstudio" / "apps"
    logger = _configure_logger(repo_root, args.log_file)
    report = check_repository(
        repo_root=repo_root,
        apps_root=apps_root,
        app_filter=args.app,
        apply=args.apply,
        logger=logger,
    )
    if args.as_json:
        print(json.dumps(report.to_dict(include_diff=args.diff), indent=2, ensure_ascii=False))
    else:
        for finding in report.findings:
            print(format_finding(finding))
        for plan in report.plans:
            for change in plan.changes:
                print(f"CHANGE [{plan.app}] {change}")
        if args.diff:
            diff = render_diffs(report.plans)
            if diff:
                print(diff, end="" if diff.endswith("\n") else "\n")
        print(
            f"Summary: {report.fail_count} FAIL, "
            f"{report.warning_count} WARN, {report.info_count} INFO"
        )
    return 1 if report.fail_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
