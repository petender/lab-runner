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

    def test_ownership_mismatch_prevents_cleanup(self):
        def mismatch(args, *other, **kwargs):
            if args[:3] == ["az", "group", "show"]:
                return "someone-else"
            return self.execute(args, *other, **kwargs)
        with patch.object(runner, "command", side_effect=mismatch):
            with self.assertRaisesRegex(RuntimeError, "Ownership tag mismatch"):
                runner.lifecycle(self.args)
        self.assertTrue(self.group_exists)


if __name__ == "__main__":
    unittest.main()
