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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


HOME = Path(__file__).resolve().parent
DATA = HOME / ".runner"


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


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
    return manifest


def run_commands(root: Path, manifest: dict, phase: str, env: dict | None = None) -> None:
    work = inside(root, manifest["working_directory"])
    python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    for index, args in enumerate(manifest.get(phase, []), 1):
        rendered = [arg.replace("{python}", str(python)) for arg in args]
        command(rendered, work, root / ".runner" / "logs" / f"{phase}-{index}.log", env)


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
    try:
        compose(root, manifest)
        command([str(args.python.resolve()), "-m", "venv", str(root / ".venv")], root)
        python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        requirements = manifest.get("requirements")
        if requirements:
            command([str(python), "-m", "pip", "install", "-r",
                     str(inside(root, requirements))], root,
                    root / ".runner" / "logs" / "dependencies.log")
        command([str(python), "-m", "pip", "check"], root)
        run_commands(root, manifest, "local")
    except (OSError, RuntimeError, ValueError, IndexError) as exc:
        write(root / ".runner" / "state.json", {
            "source": str(source), "commit": commit, "local": "failed", "error": str(exc),
        })
        raise
    write(root / ".runner" / "state.json", {
        "source": str(source), "commit": commit, "local": "passed",
    })
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
        azd("env", "new", approval["environment"], "--subscription", subscription,
            "--location", approval["location"], "--no-prompt")
        values = {
            **manifest["azure"].get("values", {}),
            "AZURE_SUBSCRIPTION_ID": subscription,
            "AZURE_LOCATION": approval["location"], "AZURE_RESOURCE_GROUP": group,
        }
        for key, value in values.items():
            azd("env", "set", key, str(value), private=True)
        # What-if can require an existing resource group.
        owned = True
        az("group", "create", "-n", group, "-l", approval["location"],
           "--tags", f"labRunnerApproval={approval['token']}", "-o", "none")
        azd("provision", "--preview", "--no-prompt")
        state["phases"]["preview"] = "passed"
        azd("provision", "--no-prompt")
        state["phases"]["provision"] = "passed"
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
        state["evidence"] = validate_evidence(root, manifest)
        state["phases"]["live"] = "passed"
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        errors.append(str(exc))
    except KeyboardInterrupt:
        errors.append("Interrupted by operator.")
    finally:
        if owned:
            try:
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
    if errors:
        raise RuntimeError("; ".join(errors))
    print("Automated lifecycle PASS. Review any manifest manual checks separately.")


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
    for item in (prep, planning, run):
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
