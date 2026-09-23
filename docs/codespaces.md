# Codespaces development environment

This is a temporary Linux work PC for the existing orchestrator, not a
24-hour service or a new scientific workflow. The same Git commit should run
on the Windows notebook and in a Codespace. The dev container uses Python
3.11, Node.js 22, and the repository's `.[studio]` dependency group; its
post-create step installs Codex CLI. No model, dataset, or experiment is run
during setup.
The pinned Codex CLI version is `0.156.1`, which includes the GPT-6 Sol/Luna
model catalog according to the [Codex release notes](https://learn.chatgpt.com/docs/changelog).
New orchestration runs request those models; actual account access still
depends on rollout and workspace settings. Do not substitute a different
model silently if the account cannot use one.

## Before creating a Codespace

1. Commit and push the source and small, reviewed receipts you need. Confirm
   that `.env`, Codex authentication files, SQLite run directories, datasets,
   checkpoints, models, and large evidence are **not** staged. Use a separate
   store for large artifacts, and commit only a manifest, hash, or location if
   that information is safe to publish.
2. In GitHub's repository or account Codespaces Secrets, configure
   `LANGSMITH_API_KEY` if remote tracing is desired. Set
   `LANGSMITH_TRACING=true` and, optionally, `LANGSMITH_PROJECT` there too.
   Without a key, the orchestration and Studio topology preview still run;
   remote LangSmith traces do not upload. Never commit a populated `.env` or
   copy authentication files from the notebook.
3. Check your own Codespaces quota/billing settings before creating a machine.
   Stop or delete an idle Codespace to avoid unnecessary usage.

Create a Codespace from the intended branch. GitHub reads
`.devcontainer/devcontainer.json` and runs `.devcontainer/post-create.sh` on
first creation/rebuild. Wait for post-create to finish, then check in its
terminal:

```bash
.venv/bin/python --version
node --version
npm --version
codex --version
git --version
.venv/bin/langgraph --help
codex login status
```

Codex login is a separate **human** action. Prefer ChatGPT account sign-in;
in a headless Codespace use `codex login --device-auth` if your account or
workspace allows device-code sign-in, and complete the displayed flow yourself.
Do not paste an access token into chat, a shell command, or Git. Verify
`codex login status` afterward. An API key is not required for ChatGPT login;
do not switch to API-key billing without explicit approval.

## Read-only Studio view and local checks

The Studio entry point is deliberately a topology-only preview. Its nodes
reject execution; a Studio click must not run Astra, Sol, Luna, Reviewer, or
project experiments. From the repository root in the Codespace:

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m astra_orchestrator dry-run \
  --output .astra-orchestrator/codespaces-dry-run
.venv/bin/langgraph dev --host 0.0.0.0 --port 2024 --no-browser
```

Use a new dry-run output name for each repeat; the command will not overwrite
an existing receipt. The dry run uses scripted roles only. In the Codespace
**Ports** panel, confirm port 2024 is forwarded and **Private**, then open its
forwarded URL while signed in to GitHub. Check its `/ok` endpoint and use the
Studio URL printed by `langgraph dev`, replacing a localhost `baseUrl` with
the Codespace forwarded URL if needed. Studio should show the
`astra_orchestrator` graph. If direct cross-origin Studio access fails with a
private forwarded URL, keep the port private and use a local authenticated
port forward (for example, the Codespaces client/CLI) to `localhost:2024`;
do **not** make the Agent Server public to work around an access error.

The development server requires an active Codespace. Stop it with Ctrl+C.
LangSmith tracing of a real orchestration run is separate from Studio:
invoke the documented `start`/`resume` CLI only when actual project work is
authorized. A scripted dry run does not contact Codex or create live role
traces. See [the orchestration guide](orchestration.md#langsmith-tracing) for
which role metadata is available; Codex token counts and cost appear only
when the CLI reports them reliably. No usage values are estimated.

## Notebook ↔ GitHub ↔ Codespace handoff

Before switching machines, commit and push selected source, configuration,
tests, documentation, and small nonsensitive receipts from the current
machine. On the other machine, pull that commit before continuing work. Push
again before leaving the Codespace, then pull on the notebook. Inspect
`git status` and the staged diff each time; do not use `git add .` when the
worktree contains private or large artifacts. The existing branch workflow is
sufficient unless concurrent edits make a separate branch useful.

Git transports committed files, **not** a running orchestrator session.
SQLite checkpoints, Codex session IDs, credentials, and ignored run directories
stay on the machine where they were created. Resume a paused run on that same
machine with its original `--run-dir`; do not start a second run against the
same project state or assume `git pull` transferred an in-flight checkpoint.
When a Codespace is stopped and restarted, check that its run directory is
still present before resuming. A rebuild or deletion may remove local state;
preserve required receipts separately without putting secrets or large
artifacts in Git. Cross-machine live-run migration is not implemented.

Repository code uses the launch `--project-root` and the specification's
relative paths; no Windows `D:\\...` path is required by the runtime. Supply
Linux paths inside Codespaces and Windows paths on the notebook. Keep the
orchestrator `--run-dir` outside Sol's writable project root on either system.

## Verification boundary

Local tests can establish syntax, package installation, graph preview, and
scripted routing. Only a newly created, authenticated Codespace can establish
the complete remote acceptance chain: post-create success, ChatGPT login,
private forwarded-port access from the notebook browser, Studio connection,
and a push/pull handoff. Record those observations separately; do not call
unverified remote steps complete. The current local-versus-remote status is in
[the verification receipt](codespaces-verification.md).
