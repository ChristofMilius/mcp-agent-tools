from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "launch_check.py"
SPEC = importlib.util.spec_from_file_location("launch_check_test_subject", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("could not load launch checker")
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class LaunchCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "coordinator"
        self.apps_root = Path(self.temp_dir.name) / "lmstudio" / "apps"
        self.root.mkdir()
        self.apps_root.mkdir(parents=True)
        self.write_inventory(self.root, playwright_main=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_inventory(self, root: Path, playwright_main: bool) -> None:
        tools = {
            "mcp_agent_docparser": ("mcp-agent-docparser", "mcp_agent_docparser:main"),
            "mcp_agent_mail": ("mcp-agent-mail", "mcp_agent_mail:main"),
            "mcp_agent_transcriber": ("mcp-agent-transcriber", "mcp_agent_transcriber:main"),
            "mcp_agent_playwright": ("mcp-agent-playwright", "mcp_agent_playwright.server:main"),
            "mcp_agent_openjev": ("mcp-agent-openjev", "mcp_agent_openjev:main"),
        }
        lines = ["schema = 1", ""]
        for directory, (script, target) in tools.items():
            lines.extend(
                [
                    "[[tool]]",
                    f'name = "{script}"',
                    f'directory = "{directory}"',
                    f'repository = "https://example.invalid/{directory}.git"',
                    'branch = "master"',
                    f'project_name = "{script}"',
                    f'script_name = "{script}"',
                    f'target_module = "{target}"',
                    "",
                ]
            )
            tool_root = root / directory
            package = script.replace("-", "_")
            (tool_root / "src" / package).mkdir(parents=True, exist_ok=True)
            (tool_root / ".venv" / "Scripts").mkdir(parents=True, exist_ok=True)
            (tool_root / "pyproject.toml").write_text(
                "[project]\n"
                f'name = "{script}"\n'
                "[project.scripts]\n"
                f'{script} = "{target}"\n',
                encoding="utf-8",
            )
            main_path = tool_root / "src" / package / "__main__.py"
            if directory != "mcp_agent_playwright" or playwright_main:
                main_path.write_text("", encoding="utf-8")
            elif main_path.exists():
                main_path.unlink()
            (tool_root / ".venv" / "Scripts" / f"{script}.exe").write_text("", encoding="utf-8")
        (root / "stack.toml").write_text("\n".join(lines), encoding="utf-8")

    def write_config(self, fixture_name: str, app: str = "bionic") -> Path:
        fixture = Path(__file__).resolve().parent / "fixtures" / fixture_name
        text = fixture.read_text(encoding="utf-8")
        replacement = str(self.root).replace("\\", "/")
        text = text.replace("C:/launch-check-fixture/coordinator", replacement)
        path = self.apps_root / app / ".internal" / "ng-mcp.json"
        path.parent.mkdir(parents=True)
        path.write_text(text, encoding="utf-8")
        return path

    def check(self, help_runner=None):
        return MODULE.check_repository(
            self.root,
            self.apps_root,
            help_runner=help_runner,
        )

    @staticmethod
    def help_ok(spec, launch_args, subcommand):
        output = "usage: tool\ncommands:\n"
        if subcommand:
            output += f"  {subcommand}\n"
        return MODULE.HelpResult(True, output=output)

    def codes(self, report, severity=None):
        return [
            finding.code
            for finding in report.findings
            if severity is None or finding.severity == severity
        ]

    def test_console_script_typo_reports_near_match(self) -> None:
        self.write_config("console_script_typo.json")
        report = self.check()
        self.assertIn("typo", self.codes(report, "FAIL"))
        typo = next(finding for finding in report.findings if finding.code == "typo")
        self.assertEqual(typo.suggestion["args"][-1], "mcp-agent-docparser")

    def test_module_form_checks_subcommand_against_server_help(self) -> None:
        self.write_config("module_form.json")
        calls = []

        def help_runner(spec, launch_args, subcommand):
            calls.append((tuple(launch_args), subcommand))
            return self.help_ok(spec, launch_args, subcommand)

        report = self.check(help_runner=help_runner)
        self.assertNotIn("subcommand-unverified", self.codes(report, "FAIL"))
        self.assertNotIn("unknown-subcommand", self.codes(report, "FAIL"))
        self.assertIn((("python", "-m", "mcp_agent_openjev"), "serve"), calls)

    def test_module_form_is_rejected_when_playwright_has_no_main(self) -> None:
        self.write_inventory(self.root, playwright_main=False)
        self.write_config("playwright_no_main.json")
        report = self.check()
        self.assertIn("module-form-unavailable", self.codes(report, "FAIL"))

    def test_missing_tool_is_informational(self) -> None:
        self.write_config("missing_tool.json")
        report = self.check()
        self.assertEqual(report.fail_count, 0)
        self.assertIn("tool-not-configured", self.codes(report, "INFO"))

    def test_manifest_alone_checks_configs_without_tool_sources(self) -> None:
        for child in self.root.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
        self.write_config("console_script_typo.json")
        report = self.check()
        self.assertIn("inventory-source-absent", self.codes(report, "INFO"))
        self.assertNotIn("inventory-missing-tool", self.codes(report, "FAIL"))
        typo = next(finding for finding in report.findings if finding.code == "typo")
        self.assertEqual(typo.suggestion["args"][-1], "mcp-agent-docparser")

    def test_manifest_row_missing_launch_fields_fails(self) -> None:
        manifest = self.root / "stack.toml"
        text = manifest.read_text(encoding="utf-8")
        manifest.write_text(text.replace('script_name = "mcp-agent-mail"\n', ""), encoding="utf-8")
        report = self.check()
        self.assertIn("inventory-manifest", self.codes(report, "FAIL"))

    def test_manifest_rejects_unsafe_directory(self) -> None:
        manifest = self.root / "stack.toml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                'directory = "mcp_agent_mail"', 'directory = "../escape"'
            ),
            encoding="utf-8",
        )
        report = self.check()
        self.assertIn("inventory-manifest", self.codes(report, "FAIL"))

    def test_servers_key_is_accepted(self) -> None:
        path = self.write_config("module_form.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["servers"] = data.pop("mcpServers")
        path.write_text(json.dumps(data), encoding="utf-8")
        report = self.check(help_runner=self.help_ok)
        self.assertNotIn("malformed-config", self.codes(report, "FAIL"))
        self.assertEqual(report.fail_count, 0)

    def test_disabled_entry_fails(self) -> None:
        self.write_config("disabled.json")
        report = self.check()
        self.assertIn("disabled-entry", self.codes(report, "FAIL"))

    def test_malformed_json_fails_without_raising(self) -> None:
        self.write_config("malformed.json")
        report = self.check()
        self.assertIn("malformed-json", self.codes(report, "FAIL"))

    def test_output_redacts_local_home_path(self) -> None:
        home = str(Path.home())
        finding = MODULE.Finding("INFO", "test", f"path={home}\\config")
        output = json.dumps(finding.to_dict())
        self.assertNotIn(home, output)
        self.assertIn("~", output)

    def test_apply_repairs_typo_and_preserves_id(self) -> None:
        path = self.write_config("console_script_typo.json")
        before = json.loads(path.read_text(encoding="utf-8"))
        report = MODULE.check_repository(self.root, self.apps_root, apply=True)
        after = json.loads(path.read_text(encoding="utf-8"))
        before_id = before["mcpServers"][0]["id"]
        after_id = after["mcpServers"][0]["id"]
        self.assertEqual(after_id, before_id)
        self.assertEqual(after["mcpServers"][0]["connection"]["command"], "uv")
        self.assertEqual(after["mcpServers"][0]["connection"]["args"][-1], "mcp-agent-docparser")
        self.assertEqual(report.fail_count, 0)

    def test_diff_does_not_write_config(self) -> None:
        path = self.write_config("console_script_typo.json")
        before = path.read_bytes()
        report = self.check()
        diff = MODULE.render_diffs(report.plans)
        self.assertIn("mcp-agent-docparser", diff)
        self.assertEqual(path.read_bytes(), before)
        json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
