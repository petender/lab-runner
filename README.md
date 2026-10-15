# Lab Runner

A reusable Windows-first lab testing core and VS Code Copilot agent/skill.
Fresh standalone history; no real lab content, Azure account data, or lab adapters
are bundled. Python standard library only; Python 3.11+ is required.

## Prerequisites

- [Python](https://www.python.org/downloads/): select a version/architecture
  compatible with your lab's dependencies. Python 3.13 x64 is a practical default.
- [Git](https://git-scm.com/downloads).
- Windows PowerShell 5.1+ for labs that use it; scripts are not activated or
  execution-policy bypassed by the core.
- For Azure labs: [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli),
  [azd](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd),
  and Bicep (`az bicep install` after installing Azure CLI).
- VS Code with an enabled Copilot setup for chat use. Open this repository as the
  workspace. Select **Lab Runner** or invoke `/lab-runner`; reload VS Code if needed.

Install tools using approved package sources, restart the terminal for PATH changes,
then run the prerequisite script:

```powershell
python .\lab_runner.py check --python 'C:\path\to\python.exe' --azure
```

Without `--azure`, the check does not require Azure tools. The check never installs
software, logs in, or changes policy. Tool installation remains a manual operation.

## Local preparation

Ask the agent to test a lab and supply its source location and manifest. Alternatively:

```powershell
python .\lab_runner.py prepare --source 'C:\sources\lab' `
  --manifest 'C:\adapters\my-lab.json' --root 'C:\lab-runs\unique-run' `
  --python 'C:\path\to\python.exe'
```

The runner clones committed source, copies configured instruction snippets, creates
an isolated venv, installs declared requirements, checks dependencies, and runs local
commands. Add `--commit '<full-sha>'` to require a specific source HEAD. The target
must not exist and must be outside the source repository. Source changes are never
committed or copied implicitly. No activation or manual snippet copying is needed.

## View runs in the same VS Code window

Open [lab-runner.code-workspace](lab-runner.code-workspace) to use a multi-root
workspace with **Lab Runner** and **Lab Runs** in one Explorer. By default, the
second folder resolves to the sibling `..\lab-runs` directory (`C:\lab-runs`
when the runner is in `C:\lab-runner`). Create that directory if it does not
exist yet, or edit the workspace folder path if you store runs elsewhere.

To add the runs directory to an already-open VS Code window without replacing
its existing workspace folders:

```powershell
code --reuse-window --add 'C:\lab-runs'
```

Add the parent directory once, not each clone. Every new run under that directory
automatically appears in Explorer, including its source, `.runner\report.md`,
logs, and captured evidence. VS Code's **File > Save Workspace As** preserves
your current folder layout for reopening later; the supplied workspace file is
also reusable. Runs stored outside the configured parent will not appear there.
This only changes workspace visibility: it does not move artifacts, execute lab
code, change approval requirements, or commit disposable clones into this repo.

## Manifest contract

See [the synthetic manifest](examples/manifest.json) and
[synthetic source](examples/synthetic-source). No repository layout is assumed.

- `version`: currently `1`.
- `working_directory`: path relative to the disposable clone.
- `requirements`: optional requirements file relative to the clone.
- `compose`: optional operations with `target`, `instructions`, zero-based Markdown
  fenced `block`, and exact unique `placeholder`. The literal snippet replaces the
  placeholder; authors must preserve indentation in the instructions/placeholder.
- `local`: argument arrays; every command must exit zero. `{python}` selects the
  clone-local venv interpreter. No implicit shell expansion. To run PowerShell,
  declare its executable and arguments explicitly without bypassing policy.
- `live`: argument arrays executed after provisioning, all requiring exit zero.
  Include post-deployment readiness and semantic/service acceptance commands here.
- `evidence`: objects with `path` relative to clone and `format` (`json` or `text`).
  JSON supports `required_keys` and top-level `equals`. Evidence must be new and
  nonempty. Shape checks do not prove answer quality or service-side behavior.
- `manual_checks`: acceptance criteria that cannot be automatically verified.
- `azure`: optional object containing `values` (non-secret azd input settings) and
  `export` (allowlisted deployment output names passed to live subprocesses).
- `reporting`: optional `title` and `steps` object mapping runner step IDs to
  `{"task": "Task 4, step 2", "label": "Run completed Python solution"}`.
  See the synthetic manifest for a task-mapped example. Unknown step IDs are rejected.

Example Azure extension (not deployable on its own):

```json
{
  "azure": {"values": {}, "export": ["PROJECT_ENDPOINT"]},
  "live": [["{python}", "run_lab.py"]],
  "evidence": [
    {"path": "evidence.json", "format": "json",
     "required_keys": ["status"], "equals": {"status": "passed"}}
  ],
  "manual_checks": ["Inspect service traces if required by this lab."]
}
```

The lab's own `azure.yaml` and Bicep define infrastructure. No fixed model policy,
SDK, infrastructure, source layout, or scenario validator is embedded in the core.
Manifests execute trusted code: inspect them and lab sources before execution.
Use synthetic data. Commands must not print credentials or sensitive content;
normal command logs are captured locally.

## Azure cycle and approval

Authenticate once per cycle and confirm the same subscription across its labs:

```powershell
python .\lab_runner.py cycle --source 'C:\sources\lab' `
  --subscription '<confirmed-id>' --authenticate
```

Complete Azure CLI and azd browser sign-in yourself. If Azure CLI selects the wrong
subscription, use `az account set --subscription '<confirmed-id>'` and retry.
Omit `--authenticate` to reuse existing valid sign-ins. Sign-in status is checked.
Cycle metadata is stored under `.runner` without credentials. Explicit `--replace`
starts another cycle and invalidates plans. Expired credentials require renewed
sign-in to the same target, not silently switching identity.

```powershell
python .\lab_runner.py plan --root 'C:\lab-runs\unique-run' `
  --location '<supported-region>' --group '<new-dedicated-group>' `
  --environment '<unique-environment>'

# Only AFTER operator consent to the displayed full lifecycle:
python .\lab_runner.py run --root 'C:\lab-runs\unique-run' --approve '<token>'
```

Approval binds the cycle/target, clone, runner, manifest, and source content.
It expires after 24 hours and is consumed once. Authentication is not approval.
Preview may need an existing resource group, so approved group creation precedes
preview. Preview failure blocks provisioning and still triggers cleanup.
Only NEW dedicated groups are accepted. The runner tags its group and checks
ownership before destructive cleanup. `azd down --purge --force` can permanently
delete resources; inspect trusted templates and ensure they target only that group.
Templates must not create external/subscription-scoped resources: those are outside
this core's ownership/cleanup guarantee and require a separate adapter/workflow.

Deployment outputs are allowlisted into subprocess environment variables; no
environment file or raw output export is logged. Lab scripts can load values with
`os.environ`. Do not put secrets into manifest settings.

State, phase logs, evidence digests, and failures persist inside the clone's
`.runner` folder. Cleanup is attempted after group creation begins, even on failure.
The group must be absent afterward for cleanup to pass. Process termination,
authentication loss, locks, or provider failures may leave billable resources:
inspect Azure and resolve them explicitly. Manual checks remain manual.

## Per-lab Markdown reports

Every prepared lab sequence automatically writes `.runner\report.md`, backed by
`.runner\results.json`. The report updates as each operation starts and finishes,
including failures, interruptions, and cleanup on failure. Each fresh clone has its
own report; later labs do not overwrite earlier reports. Reports and logs remain
local to the disposable clone and must not be committed into the lab source.

Reports include source commit, cloud target (without approval tokens), task/step
mapping, actual command, UTC start/finish times, status, error details, log links,
validated evidence digests, and unchecked manual acceptance criteria. Private azd
settings and deployment output values are withheld. Manifest commands must not
contain secrets because their argument arrays are included in the report.

Use these IDs in `reporting.steps`:

| Step IDs | Operations |
|---|---|
| `compose` | Built-in snippet composition, if declared |
| `venv`, `dependencies`, `dependency-check` | Venv creation, optional requirements install, pip check |
| `local-1`, `local-2`, ... | Local commands, in manifest order |
| `env-new`, `env-settings`, `group-create` | azd environment, settings, dedicated group creation |
| `preview`, `provision`, `outputs` | Infrastructure preview, provisioning, allowlisted output loading |
| `live-1`, `live-2`, ... | Live commands, in manifest order |
| `evidence`, `cleanup` | Evidence validation; purge and verified group absence |

Map adapter-based snippet composition to its `local-N` command, not `compose`.
For each lab adapter, map its commands to the actual guide tasks/steps and declare
unexecuted guide acceptance checks in `manual_checks`; do not reuse another lab's
task numbers or infer them from command names.
Map an explicit `azd deploy` command to its declared `live-N` step; the core runs
`azd provision`, not `azd deploy`, and does not claim an unexecuted deploy passed.
Include manual or unautomated guide steps in `manual_checks` so owners can see
what is not covered. A successful command is not proof of answer quality,
service-side branch isolation, or portal checks.

Statuses are PASSED, FAILED, RUNNING, and NOT RUN (for planned operations with no
recorded execution). A RUNNING row can survive abrupt process termination; it is
not a pass. Cleanup failures prominently warn of possible continuing charges.

Regenerate a report without rerunning commands or accessing Azure:

```powershell
python .\lab_runner.py report --root 'C:\lab-runs\unique-run'
```

For runs made before step recording was added, the report preserves known phase
outcomes but marks individual steps NOT RECORDED. It does not invent timestamps
or infer successful commands from the presence of a log file. Validation or
approval failures before execution begins do not create step results. Existing
approval plans become invalid after changing runner code; generate a new plan
before the next approved cloud run.

Commands are serialized by a local operation lock. After an unclean termination,
inspect for an active runner before removing only `.runner\operation.lock`.

## Tests

```powershell
python -m unittest discover -s tests -v
```

Synthetic local integration and mocked cloud tests do not prove any real Azure
deployment works. No real cloud deployment has been validated for this generic core.
