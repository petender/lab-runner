import argparse
import copy
import json
import tempfile
import unittest
from pathlib import Path

import copilot_usage
import lab_runner as runner


def span(identifier="request-1", counts=True, model="test-model"):
    attributes = [
        {"key": "gen_ai.request.model", "value": {"stringValue": model}},
        {"key": "gen_ai.input.messages", "value": {"stringValue": "PRIVATE PROMPT"}},
    ]
    if counts:
        attributes += [
            {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "120"}},
            {"key": "gen_ai.usage.output_tokens", "value": {"intValue": 30}},
            {"key": "gen_ai.usage.cache_read.input_tokens", "value": {"intValue": "40"}},
        ]
    return {"traceId": "test-trace", "spanId": identifier, "attributes": attributes}


def export(*spans):
    return {"resourceSpans": [{"scopeSpans": [{"spans": list(spans)}]}]}


class UsageTests(unittest.TestCase):
    def test_counts_cache_and_duplicate_spans(self):
        records, missing = copilot_usage.extract_requests(export(span(), span()))
        self.assertEqual(len(records), 1)
        self.assertEqual(missing, 0)
        totals = copilot_usage.summarize(records)["test-model"]
        self.assertEqual(totals["input_tokens"], {"recorded": 120, "missing": 0})
        self.assertEqual(totals["output_tokens"]["recorded"], 30)
        self.assertEqual(totals["cache_read_tokens"]["recorded"], 40)
        self.assertEqual(totals["cache_creation_tokens"], {"recorded": 0, "missing": 1})
        self.assertNotIn("PRIVATE PROMPT", json.dumps(records))

    def test_missing_is_not_zero_and_models_remain_separate(self):
        records, missing = copilot_usage.extract_requests(
            export(span(), span("request-2", counts=False, model="another-model"))
        )
        self.assertEqual(missing, 1)
        self.assertIsNone(records[1]["input_tokens"])
        totals = copilot_usage.summarize(records)
        self.assertEqual(totals["another-model"]["input_tokens"]["missing"], 1)

    def test_zero_is_a_valid_recorded_count(self):
        item = span()
        item["attributes"][2]["value"] = {"intValue": "0"}
        records, missing = copilot_usage.extract_requests(export(item))
        self.assertEqual(records[0]["input_tokens"], 0)
        self.assertEqual(missing, 0)

    def test_invalid_counts_rejected(self):
        for value in (-1, True, "1.5", "NaN", {"doubleValue": 12}):
            with self.subTest(value=value):
                item = span()
                item["attributes"][2]["value"] = (
                    value if isinstance(value, dict) else {"intValue": value}
                )
                with self.assertRaises(ValueError):
                    copilot_usage.extract_requests(export(item))

    def test_unsupported_empty_and_malformed_exports_rejected(self):
        for value in ([], {}, export(), {"resourceSpans": [{}]},
                      export({"traceId": "x", "attributes": {}})):
            with self.subTest(value=value), self.assertRaises(ValueError):
                copilot_usage.extract_requests(value)

    def test_non_model_tool_spans_ignored(self):
        records, _ = copilot_usage.extract_requests(
            export({"name": "tool", "attributes": [
                {"key": "gen_ai.operation.name", "value": {"stringValue": "execute_tool"}},
            ]}, span())
        )
        self.assertEqual(len(records), 1)

    def test_conflicting_spans_and_duplicate_attributes_rejected(self):
        changed = copy.deepcopy(span())
        changed["attributes"][2]["value"] = {"intValue": "999"}
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            copilot_usage.extract_requests(export(span(), changed))
        item = span()
        item["attributes"].append(item["attributes"][0])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            copilot_usage.extract_requests(export(item))

    def test_import_persists_sanitized_records_updates_report_and_rejects_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner.write(root / ".runner" / "manifest.json", {
                "version": 1, "working_directory": ".", "local": [],
            })
            runner.write(root / ".runner" / "state.json", {"local": "passed"})
            source = root / "export.json"
            runner.write(source, export(span(), span("request-2", counts=False)))
            args = argparse.Namespace(
                root=root, usage_file=source, step="Task 2", session="student-01",
                usage_scope="lab",
            )
            runner.import_copilot_usage(args)
            saved = (root / ".runner" / "copilot-usage.json").read_text()
            self.assertNotIn("PRIVATE PROMPT", saved)
            self.assertNotIn(str(source), saved)
            report = (root / ".runner" / "report.md").read_text()
            self.assertIn("120 recorded; 1 unknown", report)
            self.assertIn("student-01", report)
            self.assertIn("Task 2", report)
            self.assertIn("not billing units", report)
            self.assertIn("Actual lab steps (student-facing Copilot)", report)
            self.assertIn("Lab-runner overhead (orchestration and validation)", report)
            self.assertIn("NOT RECORDED (unknown, not zero)", report)
            with self.assertRaisesRegex(ValueError, "already been imported"):
                runner.import_copilot_usage(args)
            runner.write(source, export(span("request-2", counts=False), span("request-3")))
            with self.assertRaisesRegex(ValueError, "overlaps"):
                runner.import_copilot_usage(args)
            self.assertEqual((root / ".runner" / "copilot-usage.json").read_text(), saved)
            runner.write(source, export(span("request-3")))
            args.usage_scope = "runner"
            args.session = "orchestrator-01"
            runner.import_copilot_usage(args)
            persisted = runner.read(root / ".runner" / "copilot-usage.json")["requests"]
            totals = copilot_usage.summarize(persisted)
            self.assertEqual(totals["test-model"]["requests"], 3)
            self.assertEqual([item["usage_scope"] for item in persisted], ["lab", "lab", "runner"])
            report = (root / ".runner" / "report.md").read_text()
            self.assertNotIn("| test-model | 3 |", report)
            self.assertIn("| test-model | 2 | 120 recorded; 1 unknown |", report)
            self.assertIn("| test-model | 1 | 120 recorded; 0 unknown |", report)
            # A span cannot be attributed to both categories.
            args.usage_scope = "lab"
            with self.assertRaisesRegex(ValueError, "already been imported"):
                runner.import_copilot_usage(args)

    def test_import_rejects_unprepared_run(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(OSError):
                runner.import_copilot_usage(argparse.Namespace(
                    root=Path(directory), usage_file=Path(directory) / "export.json",
                    step="Task 1", session="student", usage_scope="lab",
                ))
            self.assertFalse((Path(directory) / ".runner").exists())

    def test_unclassified_history_is_never_counted_as_lab_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner.write(root / ".runner" / "manifest.json", {
                "version": 1, "working_directory": ".", "local": [],
            })
            runner.write(root / ".runner" / "state.json", {"local": "passed"})
            records, _ = copilot_usage.extract_requests(export(span()))
            runner.write(root / ".runner" / "copilot-usage.json", {
                "version": 1, "requests": records,
                "imports": [{"step": "unknown", "session": "legacy",
                             "sha256": "test-digest", "missing_usage": 0}],
            })
            report = runner.render_report(root).read_text()
            lab_section = report.split("### Actual lab steps")[1].split("### Lab-runner")[0]
            runner_section = report.split("### Lab-runner")[1].split("### Unclassified")[0]
            self.assertIn("NOT RECORDED", lab_section)
            self.assertIn("NOT RECORDED", runner_section)
            self.assertNotIn("120", lab_section)
            self.assertNotIn("120", runner_section)
            self.assertIn("120 recorded; 0 unknown", report.split("### Unclassified")[1])


if __name__ == "__main__":
    unittest.main()
