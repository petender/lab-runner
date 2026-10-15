"""Manifest-driven disposable lab testing. No scenario-specific adapters."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from copilot_usage import extract_requests, summarize


HOME = Path(__file__).resolve().parent
DATA = HOME / ".runner"


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


def report_steps(manifest: dict) -> dict[str, str]:
    steps = {}
    if manifest.get("compose"):
        steps["compose"] = "Copy instruction snippets"
    steps["venv"] = "Create isolated Python virtual environment"
    if manifest.get("requirements"):
        steps["dependencies"] = "Install declared dependencies"
    steps["dependency-check"] = "Check installed dependency compatibility"
    for index in range(1, len(manifest.get("local", [])) + 1):
        steps[f"local-{index}"] = f"Local command {index}"
    if manifest.get("azure"):
        steps.update({
            "env-new": "Create azd environment",
            "env-settings": "Configure azd inputs",
            "group-create": "Create dedicated resource group",
            "preview": "Preview infrastructure",
            "provision": "Provision infrastructure",
            "outputs": "Load allowlisted deployment outputs",
        })
        for index in range(1, len(manifest.get("live", [])) + 1):
            steps[f"live-{index}"] = f"Live command {index}"
        steps["evidence"] = "Validate acceptance evidence"
        steps["cleanup"] = "Purge environment and verify resource group absence"
    return steps


def render_report(root: Path) -> Path:
    manifest = read(root / ".runner" / "manifest.json")
    state = read(root / ".runner" / "state.json")
    results_path = root / ".runner" / "results.json"
    results = read(results_path) if results_path.exists() else {}
    reporting = manifest.get("reporting", {})

    def cell(value: Any) -> str:
        return (str(value).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace("|", "\\|")
                .replace("`", "\\`").replace("\r", "").replace("\n", "<br>"))

    lines = [
        f"# {cell(reporting.get('title', 'Lab execution report'))}", "",
        f"- Generated (UTC): {datetime.now(timezone.utc).isoformat()}",
        f"- Source: {cell(state.get('source', 'Not recorded'))}",
        f"- Source commit: {cell(state.get('commit', 'Not recorded'))}",
        f"- Disposable clone: {cell(root)}",
        f"- Local preparation: **{cell(state.get('local', 'not run')).upper()}**",
        f"- Cloud lifecycle: **{cell(state.get('lifecycle', 'not run')).upper()}**",
    ]
    target = state.get("target", {})
    if target:
        cycle = target["cycle"]
        for label, value in (
            ("Subscription", cycle["subscription"]), ("Tenant", cycle["tenant"]),
            ("Region", target["location"]), ("Resource group", target["group"]),
            ("azd environment", target["environment"]),
        ):
            lines.append(f"- {label}: {cell(value)}")
    lines += ["", "PASS means only the named automated operation succeeded, not full lab acceptance.",
              "NOT RUN means no recorded execution; RUNNING may indicate an interrupted process.",
              "", "## Steps", "",
              "| Step ID | Lab task / step | Operation | Status | Started / finished (UTC) | Command / details | Log |",
              "|---|---|---|---|---|---|---|"]
    for key, default_label in report_steps(manifest).items():
        mapping = reporting.get("steps", {}).get(key, {})
        result = results.get(key, {})
        status = result.get("status", "not run" if results_path.exists() else "not recorded").upper()
        timing = " / ".join(result.get(name, "-") for name in ("started", "finished"))
        details = result.get("command", "")
        if result.get("error"):
            details += "\n" + result["error"]
        log = result.get("log")
        link = f"[log]({quote(Path(log).as_posix())})" if log and (
            root / ".runner" / log
        ).is_file() else "-"
        lines.append(
            f"| {cell(key)} | {cell(mapping.get('task', 'Unmapped'))} | "
            f"{cell(mapping.get('label', default_label))} | {status} | {cell(timing)} | "
            f"{cell(details)} | {link} |"
        )
    if not results:
        lines += ["", "### Historical run", "",
                  "This run predates step recording. Individual steps above are NOT RECORDED,",
                  "not evidence of failure. Only the persisted phase outcomes below are known.",
                  "No command timestamps or individual results have been inferred from log presence.", ""]
        # Older state files only persisted successful phases.
        for name, status in state.get("phases", {}).items():
            lines.append(f"- {cell(name)}: **{cell(status).upper()}**")
    if state.get("error") or state.get("errors"):
        lines += ["", "## Issues", ""]
        for error in ([state["error"]] if state.get("error") else []) + state.get("errors", []):
            lines.append(f"- {cell(error)}")
    if target and state.get("phases", {}).get("cleanup") != "passed":
        lines += ["", "**Cleanup has not been verified. Resources may remain billable in "
                  f"{cell(target['group'])} ({cell(target['cycle']['subscription'])}).**"]
    lines += ["", "## Acceptance evidence", ""]
    for relative, record in state.get("evidence", {}).items():
        link = quote("../" + relative.replace("\\", "/"))
        lines.append(f"- [{cell(relative)}]({link}): {cell(json.dumps(record))}")
    if not state.get("evidence"):
        lines.append("No validated evidence recorded.")
    usage_path = root / ".runner" / "copilot-usage.json"
    if usage_path.exists():
        usage = read(usage_path)
        lines += ["", "## Copilot usage (imported telemetry)", "",
                  "Recorded tokens are not billing units, premium requests, or a student-cost estimate.",
                  "Lab-step usage and lab-runner overhead are reported separately, never combined. "
                  "Unexported calls and unsupported events are unknown. "
                  "Classification is supplied by the importer, not inferred from prompts.",
                  "Cache counts are separate details; do not add them to input totals.", "",
                  "Only dedicated, nonmixed sessions can be attributed to either category."]
        for scope, label in (
            ("lab", "Actual lab steps (student-facing Copilot)"),
            ("runner", "Lab-runner overhead (orchestration and validation)"),
            ("unclassified", "Unclassified historical imports (excluded from both categories)"),
        ):
            records = [item for item in usage["requests"]
                       if item.get("usage_scope", "unclassified") == scope]
            if scope == "unclassified" and not records:
                continue
            lines += ["", f"### {label}", ""]
            if not records:
                lines.append("NOT RECORDED (unknown, not zero).")
                continue
            lines += ["| Model | Recorded requests | Input tokens | Output tokens | "
                      "Cache-read tokens | Cache-creation tokens |",
                      "|---|---|---|---|---|---|"]
            for model, totals in summarize(records).items():
                values = []
                for field in ("input_tokens", "output_tokens", "cache_read_tokens",
                              "cache_creation_tokens"):
                    count = totals[field]
                    values.append(f"{count['recorded']} recorded; {count['missing']} unknown")
                lines.append(f"| {cell(model)} | {totals['requests']} | " + " | ".join(values) + " |")
        lines += ["", "### Usage imports", ""]
        for entry in usage["imports"]:
            lines.append(f"- Scope **{cell(entry.get('usage_scope', 'unclassified'))}**, "
                         f"step **{cell(entry['step'])}**, session **{cell(entry['session'])}**; "
                         f"SHA-256 `{entry['sha256']}`; "
                         f"{entry['missing_usage']} request spans missing input/output counts.")
    lines += ["", "## Manual / unverified checks", ""]
    for check in manifest.get("manual_checks", []):
        lines.append(f"- [ ] {cell(check)}")
    if not manifest.get("manual_checks"):
        lines.append("No manual checks declared; this does not establish semantic acceptance.")
    path = root / ".runner" / "report.md"
    temporary = path.with_suffix(".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


@contextmanager
def recorded_step(root: Path, key: str, description: str = "", log: Path | None = None):
    path = root / ".runner" / "results.json"
    results = read(path) if path.exists() else {}
    entry = {"status": "running", "started": datetime.now(timezone.utc).isoformat(),
             "command": description}
    if log:
        entry["log"] = str(log.relative_to(root / ".runner"))
    results[key] = entry
    write(path, results)
    if (root / ".runner" / "manifest.json").exists():
        render_report(root)
    try:
        yield
    except (OSError, RuntimeError, ValueError, KeyError, IndexError, KeyboardInterrupt) as exc:
        entry.update(status="failed", error=str(exc) or "Interrupted by operator.")
        raise
    else:
        entry["status"] = "passed"
    finally:
        entry["finished"] = datetime.now(timezone.utc).isoformat()
        write(path, results)
        if (root / ".runner" / "manifest.json").exists():
            render_report(root)


def recorded_command(root: Path, key: str, args: list[str], cwd: Path,
                     log: Path, environment: dict | None = None) -> str:
    with recorded_step(root, key, subprocess.list2cmdline(args), log):
        return command(args, cwd, log, environment)


def inside(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"Path escapes the lab root: {relative}")
    return path


def command(
    args: list[str], cwd: Path, log: Path | None = None,
    environment: dict[str, str] | None = None, private: bool = False,
) -> str:
    executable = shutil.which(args[0])
    if not executable:
        raise RuntimeError(f"Missing executable: {args[0]}")
    result = subprocess.run(
        [executable, *args[1:]], cwd=cwd, text=True, capture_output=True, check=False,
        env={**os.environ, "AZURE_DEV_USER_AGENT": "lab-runner", **(environment or {})},
    )
    if log:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            "Output withheld (may contain secrets)." if private else
            f"STDOUT\n{result.stdout}\nSTDERR\n{result.stderr}", encoding="utf-8",
        )
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {args[0]}. "
                           + (f"See {log}" if log else result.stderr.strip()))
    return result.stdout.strip()


def active_account() -> dict:
    value = json.loads(command(["az", "account", "show", "-o", "json"], HOME))
    if value["state"] != "Enabled":
        raise ValueError("Azure subscription is not enabled.")
    return value


def login(args: list[str]) -> None:
    executable = shutil.which(args[0])
    if not executable:
        raise RuntimeError(f"Missing executable: {args[0]}")
    subprocess.run([executable, *args[1:]], check=True)


def verify_cycle(cycle: dict, active: dict) -> None:
    if (cycle["subscription"].lower() != active["id"].lower()
            or cycle["tenant"].lower() != active["tenantId"].lower()):
        raise ValueError("Azure target changed. Restore the cycle subscription and tenant.")


def prerequisite_check(python: Path, azure: bool) -> None:
    version = json.loads(command(
        [str(python), "-c", "import json,sys;print(json.dumps(list(sys.version_info[:2])))"], HOME
    ))
    if tuple(version) < (3, 11):
        raise ValueError("Python 3.11+ is required.")
    print(f"PASS Python {version}")
    print(command(["git", "--version"], HOME))
    if os.name == "nt":
        print("PowerShell " + command([
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            "$PSVersionTable.PSVersion.ToString()",
        ], HOME))
    if azure:
        for args in (["az", "version"], ["azd", "version"], ["az", "bicep", "version"]):
            print(command(args, HOME))


def load_manifest(path: Path) -> dict:
    manifest = read(path)
    if manifest.get("version") != 1:
        raise ValueError("Unsupported manifest version.")
    for key in ("working_directory", "local"):
        if key not in manifest:
            raise ValueError(f"Missing manifest field: {key}")
    for phase in ("local", "live"):
        for args in manifest.get(phase, []):
            if not isinstance(args, list) or not args or not all(
                isinstance(arg, str) for arg in args
            ):
                raise ValueError(f"{phase} commands must be nonempty string argument arrays.")
    reporting = manifest.get("reporting", {})
    if not isinstance(reporting, dict) or not isinstance(reporting.get("title", ""), str):
        raise ValueError("reporting must be an object with an optional string title.")
    mappings = reporting.get("steps", {})
    if not isinstance(mappings, dict):
        raise ValueError("reporting.steps must be an object.")
    for key, mapping in mappings.items():
        if key not in report_steps(manifest):
            raise ValueError(f"Unknown reporting step: {key}")
        if not isinstance(mapping, dict) or any(
            not isinstance(value, str) for value in mapping.values()
        ) or any(name not in ("task", "label") for name in mapping):
            raise ValueError("Each reporting step supports string task and label fields.")
    return manifest


def run_commands(root: Path, manifest: dict, phase: str, env: dict | None = None) -> None:
    work = inside(root, manifest["working_directory"])
    python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    for index, args in enumerate(manifest.get(phase, []), 1):
        rendered = [arg.replace("{python}", str(python)) for arg in args]
        recorded_command(root, f"{phase}-{index}", rendered, work,
                         root / ".runner" / "logs" / f"{phase}-{index}.log", env)


def compose(root: Path, manifest: dict) -> None:
    for operation in manifest.get("compose", []):
        target = inside(root, operation["target"])
        instructions = inside(root, operation["instructions"]).read_text(encoding="utf-8")
        blocks = re.findall(r"^```[^\n]*\n(.*?)^```\s*$", instructions,
                            flags=re.MULTILINE | re.DOTALL)
        snippet = blocks[operation["block"]].rstrip("\r\n")
        placeholder = operation["placeholder"]
        content = target.read_text(encoding="utf-8")
        if not placeholder or content.count(placeholder) != 1:
            raise ValueError(f"Expected exactly one placeholder in {target}")
        target.write_text(content.replace(placeholder, snippet), encoding="utf-8")


def prepare(args: argparse.Namespace) -> None:
    source = args.source.resolve()
    root = args.root.resolve()
    if root.exists() or root.is_relative_to(source):
        raise ValueError("Use a NEW disposable clone outside the source repository.")
    manifest = load_manifest(args.manifest)
    commit = command(["git", "-C", str(source), "rev-parse", "HEAD"], HOME)
    if args.commit and args.commit != commit:
        raise ValueError("Source commit does not match --commit.")
    root.parent.mkdir(parents=True, exist_ok=True)
    command(["git", "clone", "--no-hardlinks", "--", str(source), str(root)], HOME)
    write(root / ".runner" / "state.json", {
        "source": str(source), "commit": commit, "local": "running",
    })
    write(root / ".runner" / "manifest.json", manifest)
    logdir = root / ".runner" / "logs"
    try:
        if manifest.get("compose"):
            with recorded_step(root, "compose"):
                compose(root, manifest)
        recorded_command(root, "venv",
                         [str(args.python.resolve()), "-m", "venv", str(root / ".venv")],
                         root, logdir / "venv.log")
        python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        requirements = manifest.get("requirements")
        if requirements:
            recorded_command(root, "dependencies", [str(python), "-m", "pip", "install", "-r",
                             str(inside(root, requirements))], root, logdir / "dependencies.log")
        recorded_command(root, "dependency-check", [str(python), "-m", "pip", "check"],
                         root, logdir / "dependency-check.log")
        run_commands(root, manifest, "local")
    except (OSError, RuntimeError, ValueError, KeyError, IndexError, KeyboardInterrupt) as exc:
        write(root / ".runner" / "state.json", {
            "source": str(source), "commit": commit, "local": "failed",
            "error": str(exc) or "Interrupted by operator.",
        })
        render_report(root)
        raise
    write(root / ".runner" / "state.json", {
        "source": str(source), "commit": commit, "local": "passed",
    })
    render_report(root)
    print(f"Local PASS: {root}")


def fingerprint(root: Path, manifest: dict) -> str:
    digest = hashlib.sha256(Path(__file__).read_bytes())
    digest.update(json.dumps(manifest, sort_keys=True).encode())
    paths = {path for path in root.rglob("*") if path.is_file() and not any(
        part in (".git", ".venv", ".runner", ".azure", "__pycache__")
        for part in path.relative_to(root).parts
    )}
    for path in sorted(paths):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def plan(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    if read(root / ".runner" / "state.json")["local"] != "passed":
        raise ValueError("Local validation must pass first.")
    manifest = load_manifest(root / ".runner" / "manifest.json")
    cloud = manifest.get("azure")
    if not cloud or not manifest.get("live") or not manifest.get("evidence"):
        raise ValueError("Cloud runs require azure, live, and evidence manifest sections.")
    if not isinstance(cloud, dict) or not isinstance(manifest["evidence"], list):
        raise ValueError("azure must be an object and evidence must be a list.")
    for entry in manifest["evidence"]:
        if not isinstance(entry, dict) or entry.get("format") not in ("json", "text"):
            raise ValueError("Each evidence entry needs a path and json/text format.")
        inside(root, entry["path"])
    cycle = read(DATA / "cycle.json")
    verify_cycle(cycle, active_account())
    for name in (args.location, args.group, args.environment):
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,62}", name):
            raise ValueError("Target names must be 1-63 alphanumeric/hyphen characters.")
    work = inside(root, manifest["working_directory"])
    if (work / ".azure").exists():
        raise ValueError("Use a fresh clone without an existing azd environment.")
    approval = {
        "token": secrets.token_hex(24), "used": False, "cycle": cycle,
        "root": str(root), "location": args.location, "group": args.group,
        "environment": args.environment,
        "expires": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
        "fingerprint": fingerprint(root, manifest),
        "scope": "preview, new group, provision, live commands, evidence, destructive cleanup",
    }
    write(root / ".runner" / "approval.json", approval)
    print(json.dumps(approval, indent=2))
    print("Operator approval is required. Manifest commands execute trusted code. "
          "Cloud resources/model calls are billable. Cleanup purges the dedicated group.")


def validate_evidence(root: Path, manifest: dict) -> dict:
    records = {}
    for entry in manifest["evidence"]:
        path = inside(root, entry["path"])
        content = path.read_bytes()
        if not content:
            raise ValueError(f"Empty evidence: {path}")
        if entry.get("format") == "json":
            value = json.loads(content)
            if not isinstance(value, dict):
                raise ValueError(f"Expected JSON object: {path}")
            for key in entry.get("required_keys", []):
                if key not in value:
                    raise ValueError(f"Missing evidence key: {key}")
            for key, expected in entry.get("equals", {}).items():
                if key not in value or value[key] != expected:
                    raise ValueError(f"Unexpected evidence value: {key}")
        elif entry.get("format") != "text":
            raise ValueError("Evidence format must be json or text.")
        records[entry["path"]] = {"sha256": hashlib.sha256(content).hexdigest(),
                                 "bytes": len(content)}
    return records


def lifecycle(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    manifest = load_manifest(root / ".runner" / "manifest.json")
    approval_path = root / ".runner" / "approval.json"
    approval = read(approval_path)
    if not secrets.compare_digest(approval["token"], args.approve) or approval["used"]:
        raise ValueError("Approval invalid or already consumed.")
    if datetime.now(timezone.utc) >= datetime.fromisoformat(approval["expires"]):
        raise ValueError("Approval expired.")
    cycle = read(DATA / "cycle.json")
    if (cycle != approval["cycle"] or approval["root"] != str(root)
            or fingerprint(root, manifest) != approval["fingerprint"]):
        raise ValueError("Cycle, clone, or inputs changed; plan and approve again.")
    verify_cycle(cycle, active_account())
    subscription = cycle["subscription"]
    group = approval["group"]
    logdir = root / ".runner" / "logs"

    def az(*parts: str) -> str:
        return command(["az", *parts, "--subscription", subscription], root)

    if az("group", "exists", "-n", group).lower() != "false":
        raise ValueError("Refusing to adopt or delete an existing group.")
    work = inside(root, manifest["working_directory"])
    if (work / ".azure").exists():
        raise ValueError("An azd environment already exists; prepare a new clone.")
    approval["used"] = True
    write(approval_path, approval)
    state = read(root / ".runner" / "state.json")
    state.update(lifecycle="running", target=approval, phases={})
    write(root / ".runner" / "state.json", state)
    owned = False
    errors = []

    def azd(*parts: str, private: bool = False) -> str:
        return command(["azd", *parts], work,
                       logdir / f"azd-{parts[0]}-{parts[1] if len(parts) > 1 else 'run'}.log",
                       private=private)

    try:
        with recorded_step(root, "env-new", "azd env new --no-prompt",
                           logdir / "azd-env-new.log"):
            azd("env", "new", approval["environment"], "--subscription", subscription,
                "--location", approval["location"], "--no-prompt")
        values = {
            **manifest["azure"].get("values", {}),
            "AZURE_SUBSCRIPTION_ID": subscription,
            "AZURE_LOCATION": approval["location"], "AZURE_RESOURCE_GROUP": group,
        }
        with recorded_step(root, "env-settings", "azd env set (values withheld)"):
            for key, value in values.items():
                azd("env", "set", key, str(value), private=True)
        # What-if can require an existing resource group.
        owned = True
        with recorded_step(root, "group-create", "az group create (ownership tag withheld)"):
            az("group", "create", "-n", group, "-l", approval["location"],
               "--tags", f"labRunnerApproval={approval['token']}", "-o", "none")
        with recorded_step(root, "preview", "azd provision --preview --no-prompt",
                           logdir / "azd-provision---preview.log"):
            azd("provision", "--preview", "--no-prompt")
        state["phases"]["preview"] = "passed"
        with recorded_step(root, "provision", "azd provision --no-prompt",
                           logdir / "azd-provision---no-prompt.log"):
            azd("provision", "--no-prompt")
        state["phases"]["provision"] = "passed"
        with recorded_step(root, "outputs", "azd env get-values (output withheld)"):
            outputs = json.loads(azd("env", "get-values", "--output", "json", private=True))
            env = {}
            for key in manifest["azure"].get("export", []):
                if key not in outputs or not isinstance(outputs[key], str) or not outputs[key]:
                    raise ValueError(f"Missing deployment output: {key}")
                env[key] = outputs[key]
        # Old files cannot satisfy evidence requirements for this execution.
        for entry in manifest["evidence"]:
            if inside(root, entry["path"]).exists():
                raise ValueError("Evidence already exists before live run; use a fresh clone.")
        run_commands(root, manifest, "live", env)
        with recorded_step(root, "evidence"):
            state["evidence"] = validate_evidence(root, manifest)
        state["phases"]["live"] = "passed"
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        errors.append(str(exc))
    except KeyboardInterrupt:
        errors.append("Interrupted by operator.")
    finally:
        if owned:
            try:
                with recorded_step(root, "cleanup",
                                   "Verify ownership; if group exists, "
                                   "azd down --purge --force --no-prompt; "
                                   "verify resource group absence", logdir / "azd-down---purge.log"):
                    if az("group", "exists", "-n", group).lower() == "true":
                        tag = az("group", "show", "-n", group, "--query",
                                 "tags.labRunnerApproval", "-o", "tsv")
                        if tag != approval["token"]:
                            raise ValueError("Ownership tag mismatch; refusing deletion.")
                        azd("down", "--purge", "--force", "--no-prompt")
                    if az("group", "exists", "-n", group).lower() != "false":
                        raise ValueError("Resource group still exists.")
                state["phases"]["cleanup"] = "passed"
            except (OSError, RuntimeError, ValueError) as exc:
                errors.append(f"CLEANUP FAILED: {exc}; resources may remain billable.")
        state.update(lifecycle="failed" if errors else "passed", errors=errors,
                     manual_checks=manifest.get("manual_checks", []))
        write(root / ".runner" / "state.json", state)
        render_report(root)
    if errors:
        raise RuntimeError("; ".join(errors))
    print("Automated lifecycle PASS. Review any manifest manual checks separately.")


def import_copilot_usage(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    # Require a prepared run; usage import must not create an unrelated run directory.
    load_manifest(root / ".runner" / "manifest.json")
    read(root / ".runner" / "state.json")
    if not args.step.strip() or not args.session.strip():
        raise ValueError("Usage imports require nonempty step and session labels.")
    if args.usage_scope not in ("lab", "runner"):
        raise ValueError("Usage scope must explicitly be lab or runner.")
    content = args.usage_file.read_bytes()
    requests, missing = extract_requests(json.loads(content))
    path = root / ".runner" / "copilot-usage.json"
    usage = read(path) if path.exists() else {"version": 1, "requests": [], "imports": []}
    digest = hashlib.sha256(content).hexdigest()
    if any(entry["sha256"] == digest for entry in usage["imports"]):
        raise ValueError("This export has already been imported.")
    identities = {(item["trace_id"], item["span_id"]) for item in usage["requests"]}
    if any((item["trace_id"], item["span_id"]) in identities for item in requests):
        raise ValueError("Export overlaps previously imported spans; use nonoverlapping exports.")
    usage["requests"].extend(
        {**item, "step": args.step, "session": args.session,
         "usage_scope": args.usage_scope} for item in requests
    )
    usage["imports"].append({
        "step": args.step, "session": args.session, "sha256": digest,
        "usage_scope": args.usage_scope,
        "missing_usage": missing, "imported": datetime.now(timezone.utc).isoformat(),
    })
    write(path, usage)
    print(render_report(root))
    print(f"Imported {len(requests)} request spans; {missing} missing token counts. "
          "Raw prompts, code, tool payloads and export paths were not copied.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    check = commands.add_parser("check")
    check.add_argument("--python", type=Path, default=Path(sys.executable))
    check.add_argument("--azure", action="store_true")
    cycle = commands.add_parser("cycle")
    cycle.add_argument("--source", type=Path, required=True)
    cycle.add_argument("--subscription", required=True)
    cycle.add_argument("--authenticate", action="store_true")
    cycle.add_argument("--replace", action="store_true")
    prep = commands.add_parser("prepare")
    prep.add_argument("--source", type=Path, required=True)
    prep.add_argument("--manifest", type=Path, required=True)
    prep.add_argument("--python", type=Path, default=Path(sys.executable))
    prep.add_argument("--commit")
    planning = commands.add_parser("plan")
    for name in ("location", "group", "environment"):
        planning.add_argument("--" + name, required=True)
    run = commands.add_parser("run")
    run.add_argument("--approve", required=True)
    report = commands.add_parser("report")
    usage = commands.add_parser("usage-import", help="Import scoped Copilot OTLP token telemetry")
    usage.add_argument("--usage-file", type=Path, required=True)
    usage.add_argument("--step", required=True)
    usage.add_argument("--session", required=True)
    usage.add_argument("--usage-scope", choices=("lab", "runner"), required=True,
                       help="Separate actual lab interactions from runner orchestration overhead")
    for item in (prep, planning, run, report, usage):
        item.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    lock = DATA / "operation.lock"
    acquired = False
    try:
        DATA.mkdir(exist_ok=True)
        with lock.open("x"):
            acquired = True
        if args.action == "check":
            prerequisite_check(args.python, args.azure)
        elif args.action == "usage-import":
            import_copilot_usage(args)
        elif args.action == "cycle":
            path = DATA / "cycle.json"
            if path.exists() and not args.replace:
                raise ValueError("Cycle exists. Reuse it or explicitly pass --replace.")
            source = Path(command(
                ["git", "-C", str(args.source), "rev-parse", "--show-toplevel"], HOME
            )).resolve()
            if args.authenticate:
                login(["az", "login"])
            active = active_account()
            if active["id"].lower() != args.subscription.lower():
                raise ValueError("Select the confirmed subscription with az account set.")
            if args.authenticate:
                login(["azd", "auth", "login", "--tenant-id", active["tenantId"]])
            command(["azd", "auth", "login", "--check-status"], HOME)
            write(path, {"id": str(uuid.uuid4()), "source": str(source),
                         "subscription": active["id"], "tenant": active["tenantId"]})
            print("Cycle saved; Azure credentials remain in the CLI credential stores.")
        else:
            if args.action == "report":
                print(render_report(args.root.resolve()))
            else:
                {"prepare": prepare, "plan": plan, "run": lifecycle}[args.action](args)
    except (OSError, RuntimeError, ValueError, KeyError, IndexError,
            subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        if acquired:
            lock.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
