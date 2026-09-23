# Codespaces development environment

This is a temporary Linux work PC for the existing orchestrator, not a
24-hour service or a new scientific workflow. The same Git commit should run
on the Windows notebook and in a Codespace. The dev container uses Python
3.11, Node.js 22, and the repository's `.[studio,shared]` dependency groups; its
post-create step installs Codex CLI. No model, dataset, or experiment is run
during setup.
The devcontainer also installs the SSH server feature required by
`gh codespace ssh`; this does not make the Codespace SSH port public.
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
   For cross-machine orchestration, also set `ASTRA_STATE_DATABASE_URL` to
   the new private PostgreSQL database's **direct** SSL connection URL and
   restart the Codespace so the secret reaches its environment. The Secret's
   **Name** is `ASTRA_STATE_DATABASE_URL`; its **Value** must begin with
   `postgresql://`, not `ASTRA_STATE_DATABASE_URL=postgresql://`. Set the same
   variable in the notebook's ignored `.env`; never paste it into chat.
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

For local CLI access, run `gh codespace ssh -c CODESPACE_NAME -- pwd` from the
notebook after the Codespace is running. If the SSH feature was added to an
existing Codespace, rebuild that Codespace once to apply the devcontainer
change; stopping and starting alone does not install the feature. A rebuild
can clear Codex CLI's host-local login, so check `codex login status` afterward
and complete ChatGPT device authentication again if needed.

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

Git transports committed project files, **not** an orchestrator checkpoint.
For a live cross-machine run, use the private PostgreSQL shared-resume mode
described in [the orchestration guide](orchestration.md#shared-notebook--codespace-resume).
It transfers the same run ID, LangGraph checkpoints, audit evidence, and
available Astra/Sol Codex rollouts through PostgreSQL, not the public Git
repository. Each `resume --shared` materializes a new local cache outside the
project tree. Stop the command on one machine before resuming on the other;
the database lock rejects two simultaneous controllers. Git-pull the same
project commit first. Use Google Drive only for the data and evidence needed
by the next host; do not store credentials there. Credentials remain host-local
and must be set up independently in the Codespace.

Before each notebook/Codespace switch, identify the next task's exact files,
verify that needed files arrived intact, and remove obsolete temporary copies
from the departing host. Archive a Drive file locally and compare its name,
byte count, and SHA-256 with the Drive copy or an execution receipt before
deleting it. Never delete the only copy or evidence required by a pending
review or resume. If Drive is too full for a required transfer, use verified
parts and check the reassembled file's whole-file hash. If safe cleanup and
chunking still cannot provide enough space, stop and ask the user; do not
silently buy storage or alter project evidence.
Project-scoped Drive transfers and verified cleanup do not need separate user
permission; unrelated personal files remain outside this handoff policy.

In the verified Codespace, GitHub Codespaces Secrets were present in the VS
Code terminal but not in a noninteractive `gh codespace ssh -- COMMAND` shell.
Use the Codespace terminal for shared `start`/`resume` commands and check that
`ASTRA_STATE_DATABASE_URL` is available there; do not print its value.

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
