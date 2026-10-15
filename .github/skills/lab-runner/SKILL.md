---
name: lab-runner
description: 'Run any trusted manifest-driven learner lab: discover sources, prepare disposable clones, copy instruction snippets, create Python venvs, validate locally, provision Azure with azd, capture evidence, and clean up.'
---

# Reusable lab runner

Read the workspace README and `lab_runner.py`. Execute the runner rather than
asking the user to copy/paste lab commands. Never edit the source repository.

## Workspace visibility

Before preparing the first lab in a testing session, ensure the shared runs parent
is visible alongside the runner in the current VS Code window. Default to
`C:\lab-runs` on this Windows setup unless the user chose another location.
Create the parent directory if needed; each disposable clone must still use a NEW
child directory outside the lab source repository.

If the parent is not already a workspace folder, run
`code --reuse-window --add 'C:\lab-runs'` using the actual selected parent path.
Add the parent once, not every clone: future runs, reports, logs and evidence then
appear automatically. Preserve existing workspace folders. Do not launch another
VS Code instance, switch workspaces, or overwrite the user's workspace file.
The repository's `lab-runner.code-workspace` is an optional reusable layout.

If workspace inspection is unavailable, issue the add command at most once per
testing session; VS Code deduplicates existing folders. If the `code` CLI is
unavailable or adding the folder fails, report that limitation explicitly and
offer the supplied workspace file or **File > Add Folder to Workspace**.
Do not claim the Explorer update was verified solely from the CLI exit code;
verify workspace folders when tools permit, otherwise state that the request
was sent to VS Code. Folder visibility does not grant cloud lifecycle consent.

## Lab execution

1. Ask for the lab source location and manifest. Accept an instruction folder,
   normalize to its Git repository root, and inspect the instructions and code.
   Do not assume a particular lab layout, model, API, or service. A source location
   already supplied for this cycle need not be requested again.
2. Review manifest commands, composition operations, dependencies, acceptance
   evidence, cloud settings, and manual checks. They execute trusted code. Do not
   silently replace live service validation with mocks or infer semantic success
   from nonempty output. Ask before authoring missing or ambiguous adapters.
3. Run `python .\lab_runner.py check --python '<compatible-python>'` and add
   `--azure` for cloud labs. Follow README setup instructions for missing tools.
   Ask before installing tools or changing system policy.
4. Prepare a NEW disposable clone using the manifest. Use the chosen Python
   explicitly; activation is unnecessary. Only committed sources are cloned.
   Examine failures in phase logs; never broaden allowed exit codes to hide them.
5. For the first Azure lab in a cycle, confirm subscription/tenant and ask the user
   to authenticate once. Run the `cycle` command, with `--authenticate` if needed;
   let the user complete interactive Microsoft sign-in. Never request passwords,
   tokens, or device codes in chat. Reuse valid CLI credentials and the cycle's
   target across labs. If credentials expire, request reauthentication to the
   SAME target. Ask before replacing an existing cycle.
6. Confirm region, quota, service availability, permissions, and a NEW dedicated
   resource group/environment. Generate `plan`. Present exact targets, trusted
   commands, likely billable services, and manual checks. Ask for explicit consent
   to the full lifecycle: group creation, preview, provisioning, synthetic live
   calls, evidence capture, and destructive `azd down --purge --force` cleanup
   including cleanup on failure. A plan token is not consent.
7. Only after approval, run with the printed token. Track execution to completion.
   Do not change approved inputs or target, reuse consumed approval, or suppress
   failures. Do not run multiple cycles concurrently.
8. Report preparation, preview, deployment, live evidence, and cleanup separately.
   If cleanup failed, prominently identify the target and possible continuing
   charges. Never report manual checks as passed without evidence.

Do not commit clones, environment files, credentials, local cycle state, or logs.
Keep scenario adapters outside the generic core. There are no bundled real-lab
adapters and no claim of universal lab acceptance.
