import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import lab_runner as runner


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_path_escape(self):
        with self.assertRaises(ValueError):
            runner.inside(self.root, "../outside")

    def test_composition_and_local_commands(self):
        (self.root / "instructions.md").write_text("```python\nreturn 42\n```\n")
        (self.root / "solution.py").write_text("def answer():\n    raise NotImplementedError\n")
        manifest = runner.load_manifest(runner.HOME / "examples" / "manifest.json")
        runner.compose(self.root, manifest)
        self.assertIn("    return 42", (self.root / "solution.py").read_text())
        subprocess.run([sys.executable, "-m", "venv", str(self.root / ".venv")], check=True)
        runner.run_commands(self.root, manifest, "local")

    def test_ambiguous_placeholder_rejected(self):
        (self.root / "instructions.md").write_text("```python\nreturn 42\n```\n")
        (self.root / "solution.py").write_text("raise NotImplementedError\n" * 2)
        with self.assertRaises(ValueError):
            runner.compose(self.root, runner.load_manifest(
                runner.HOME / "examples" / "manifest.json"
            ))

    def test_evidence(self):
        runner.write(self.root / "output.json", {"status": "passed"})
        manifest = {"evidence": [{"path": "output.json", "format": "json",
                                 "required_keys": ["status"], "equals": {"status": "passed"}}]}
        result = runner.validate_evidence(self.root, manifest)
        self.assertEqual(len(result["output.json"]["sha256"]), 64)
        manifest["evidence"][0]["equals"]["status"] = "failed"
        with self.assertRaises(ValueError):
            runner.validate_evidence(self.root, manifest)

    def test_nonzero_local_command_is_failure(self):
        manifest = {"working_directory": ".", "local": [[sys.executable, "-c", "exit(1)"]]}
        with self.assertRaises(RuntimeError):
            runner.run_commands(self.root, manifest, "local")

    def test_cycle_target_mismatch(self):
        with self.assertRaises(ValueError):
            runner.verify_cycle({"subscription": "a", "tenant": "t"},
                                {"id": "b", "tenantId": "t"})

    def test_manifest_argument_arrays_required(self):
        path = self.root / "manifest.json"
        runner.write(path, {"version": 1, "working_directory": ".", "local": ["echo hello"]})
        with self.assertRaises(ValueError):
            runner.load_manifest(path)

    def test_prepare_clones_composes_and_validates(self):
        source = self.root / "source"
        source.mkdir()
        fixture = runner.HOME / "examples" / "synthetic-source"
        for path in fixture.iterdir():
            (source / path.name).write_bytes(path.read_bytes())
        for args in (
            ["git", "init", "-b", "main"],
            ["git", "add", "instructions.md", "solution.py"],
            ["git", "-c", "user.name=Synthetic Test", "-c",
             "user.email=test@example.invalid", "commit", "-m", "Synthetic fixture"],
        ):
            subprocess.run(args, cwd=source, check=True, capture_output=True)
        target = self.root / "clone"
        runner.prepare(argparse.Namespace(
            source=source, root=target, manifest=runner.HOME / "examples" / "manifest.json",
            python=Path(sys.executable), commit=None,
        ))
        self.assertEqual(runner.read(target / ".runner" / "state.json")["local"], "passed")
        self.assertIn("raise NotImplementedError", (source / "solution.py").read_text())
        self.assertIn("return 42", (target / "solution.py").read_text())
        results = runner.read(target / ".runner" / "results.json")
        for key in ("compose", "venv", "dependency-check", "local-1", "local-2"):
            self.assertEqual(results[key]["status"], "passed")
            self.assertLessEqual(results[key]["started"], results[key]["finished"])
        report = (target / ".runner" / "report.md").read_text()
        self.assertIn("Task 3, step 2", report)
        self.assertIn("[log](logs/local-2.log)", report)
        self.assertIn("**PASSED**", report)

    def test_report_records_failure_and_unexecuted_steps(self):
        manifest = {
            "version": 1, "working_directory": ".",
            "local": [[sys.executable, "-c", "exit(7)"], [sys.executable, "-V"]],
            "manual_checks": ["Inspect portal | traces"],
        }
        runner.write(self.root / ".runner" / "manifest.json", manifest)
        runner.write(self.root / ".runner" / "state.json", {"local": "running"})
        with self.assertRaisesRegex(RuntimeError, "failed \\(7\\)"):
            runner.run_commands(self.root, manifest, "local")
        report = (self.root / ".runner" / "report.md").read_text()
        self.assertIn("| local-1 | Unmapped | Local command 1 | FAILED |", report)
        self.assertIn("| local-2 | Unmapped | Local command 2 | NOT RUN |", report)
        self.assertIn("- [ ] Inspect portal \\| traces", report)
        self.assertIn("Command failed (7)", report)

    def test_reporting_rejects_invalid_mapping(self):
        path = self.root / "manifest.json"
        for mapping in ({"live-1": {"task": "Not declared"}},
                        {"venv": {"task": 1}},
                        {"venv": {"unknown": "value"}}):
            runner.write(path, {"version": 1, "working_directory": ".", "local": [],
                                "reporting": {"steps": mapping}})
            with self.assertRaises(ValueError):
                runner.load_manifest(path)

    def test_historical_report_does_not_invent_steps(self):
        runner.write(self.root / ".runner" / "manifest.json", {
            "working_directory": ".", "local": [[sys.executable, "-V"]],
        })
        runner.write(self.root / ".runner" / "state.json", {
            "local": "passed", "phases": {"preview": "passed", "cleanup": "passed"},
        })
        report = runner.render_report(self.root).read_text()
        self.assertIn("NOT RECORDED", report)
        self.assertNotIn("| Local command 1 | PASSED |", report)
        self.assertIn("- cleanup: **PASSED**", report)

    def test_interrupted_step_is_failed(self):
        with self.assertRaises(KeyboardInterrupt):
            with runner.recorded_step(self.root, "venv"):
                raise KeyboardInterrupt
        result = runner.read(self.root / ".runner" / "results.json")["venv"]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "Interrupted by operator.")


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "metadata"
        self.patchers = [
            patch.object(runner, "DATA", self.data),
            patch.object(runner, "fingerprint", return_value="hash"),
            patch.object(runner, "active_account",
                         return_value={"id": "sub", "tenantId": "tenant"}),
            patch.object(runner, "run_commands"),
            patch.object(runner, "validate_evidence", return_value={"result": "digest"}),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.manifest = {
            "version": 1, "working_directory": ".", "local": [],
            "live": [["{python}", "main.py"]],
            "azure": {"values": {}, "export": ["ENDPOINT"]},
            "evidence": [{"path": "result.json", "format": "json"}],
        }
        self.cycle = {"id": "cycle", "subscription": "sub", "tenant": "tenant"}
        runner.write(self.data / "cycle.json", self.cycle)
        runner.write(self.root / ".runner" / "manifest.json", self.manifest)
        runner.write(self.root / ".runner" / "state.json", {"local": "passed"})
        self.approval = {
            "token": "approved", "used": False, "cycle": self.cycle,
            "root": str(self.root.resolve()), "location": "region", "group": "rg-new",
            "environment": "env-new", "fingerprint": "hash",
            "expires": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }
        self.save_approval()
        self.args = argparse.Namespace(root=self.root, approve="approved")
        self.group_exists = False
        self.calls = []

        def execute(args, cwd, *positional, **kwargs):
            self.calls.append(args)
            if args[:3] == ["az", "group", "exists"]:
                return "true" if self.group_exists else "false"
            if args[:3] == ["az", "group", "create"]:
                self.group_exists = True
            if args[:3] == ["az", "group", "show"]:
                return "approved"
            if args[:2] == ["azd", "down"]:
                self.group_exists = False
            if args[:3] == ["azd", "env", "get-values"]:
                return json.dumps({"ENDPOINT": "https://example.invalid"})
            return ""

        self.execute = execute

    def save_approval(self):
        runner.write(self.root / ".runner" / "approval.json", self.approval)

    def test_success_and_single_use(self):
        with patch.object(runner, "command", side_effect=self.execute):
            runner.lifecycle(self.args)
            with self.assertRaises(ValueError):
                runner.lifecycle(self.args)
        state = runner.read(self.root / ".runner" / "state.json")
        self.assertEqual(state["lifecycle"], "passed")
        self.assertEqual(state["phases"]["cleanup"], "passed")
        results = runner.read(self.root / ".runner" / "results.json")
        for key in ("env-new", "env-settings", "group-create", "preview", "provision",
                    "outputs", "evidence", "cleanup"):
            self.assertEqual(results[key]["status"], "passed")
        report = (self.root / ".runner" / "report.md").read_text()
        self.assertNotIn("approved", report)
        self.assertNotIn("https://example.invalid", report)

    def test_existing_group_never_deleted(self):
        self.group_exists = True
        with patch.object(runner, "command", side_effect=self.execute):
            with self.assertRaisesRegex(ValueError, "existing group"):
                runner.lifecycle(self.args)
        self.assertFalse(any(call[:2] == ["azd", "down"] for call in self.calls))

    def test_changed_input_stops_cloud(self):
        with patch.object(runner, "fingerprint", return_value="changed"):
            with self.assertRaisesRegex(ValueError, "inputs changed"):
                runner.lifecycle(self.args)
        self.assertEqual(self.calls, [])

    def test_expired_token(self):
        self.approval["expires"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.save_approval()
        with self.assertRaisesRegex(ValueError, "expired"):
            runner.lifecycle(self.args)

    def test_provision_failure_cleans_up(self):
        def fail(args, *other, **kwargs):
            if args[:2] == ["azd", "provision"] and "--preview" not in args:
                raise RuntimeError("provision failure")
            return self.execute(args, *other, **kwargs)
        with patch.object(runner, "command", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "provision failure"):
                runner.lifecycle(self.args)
        self.assertFalse(self.group_exists)
        results = runner.read(self.root / ".runner" / "results.json")
        self.assertEqual(results["provision"]["status"], "failed")
        self.assertEqual(results["cleanup"]["status"], "passed")
        report = (self.root / ".runner" / "report.md").read_text()
        self.assertIn("provision failure", report)
        self.assertIn("| Live command 1 | NOT RUN |", report)

    def test_ownership_mismatch_prevents_cleanup(self):
        def mismatch(args, *other, **kwargs):
            if args[:3] == ["az", "group", "show"]:
                return "someone-else"
            return self.execute(args, *other, **kwargs)
        with patch.object(runner, "command", side_effect=mismatch):
            with self.assertRaisesRegex(RuntimeError, "Ownership tag mismatch"):
                runner.lifecycle(self.args)
        self.assertTrue(self.group_exists)
        report = (self.root / ".runner" / "report.md").read_text()
        self.assertIn("CLEANUP FAILED", report)
        self.assertIn("Resources may remain billable in rg-new", report)


if __name__ == "__main__":
    unittest.main()
