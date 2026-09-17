# Measurement and provenance

Use this reference when recording a PM session's provenance or evaluating a
proposed workflow change. It does not activate PM, grant authority, or replace
execution ownership. Do not inspect raw rollouts on each progress update.

## Record an activation or changed provenance

The recorder appends one JSON object to an output file. Use a known session ID
and an existing file that contains the explicit activation evidence. Record
`unknown` when activation cannot be established. An evidence file's existence
is not proof that its contents authorize PM or external operations.

```text
python .agents/skills/astra-pm/scripts/record_run_metadata.py --project-root . --session-id <known-id> --pm-status active --activation-evidence <evidence-file> --output <private-metadata.jsonl>
```

Optional `--rollout <session.jsonl>` records model observations with source
locations. Without it, observed model is unknown; do not substitute a configured
or declared model. `--declared-model` and `--declared-role` are declarations only.
Current hashes describe files at capture time, not historical versions or proof
that the model loaded them. Record once at activation and again only when the
effective provenance changes. A resumed session need not rewrite identical
metadata. Record errors truthfully; never replace an unknown with a guess.

Keep the complete metadata file on disk. In context, retain only its path and
capture status. Do not create a separate progress diary or rewrite old records.

## Freeze and analyze evidence

`freeze_evidence.py` copies supplied files into a fresh private directory and
writes a hash manifest. It refuses to overwrite an existing directory. Raw
rollouts may contain private context and must not enter the public skill repo.

```text
python .agents/skills/astra-pm/scripts/freeze_evidence.py --session <root.jsonl> --session <child.jsonl> --file skill=<SKILL.md> --output <new-private-directory>
python .agents/skills/astra-pm/scripts/analyze_rollout.py --session <frozen-root.jsonl> --session <frozen-child.jsonl> --root-thread-id <root-id> --cutoff <UTC-ISO-time> --output <report.json>
```

Supply all relevant descendants and fix the cutoff before comparing policies.
The analyzer cannot discover omitted sessions by itself. A case-specific polling
selector is an explicit measurement assumption, not a universal classification.

Separate cached/noncached input, output, compaction and resume costs. A cache
miss is not proof of new task content or avoidable waste. Never sum cumulative
token counters or add reasoning/cached subsets a second time. Report missing
latency/recovery ground truth as unavailable. Preserve the price model and its
date if converting measurements to money.

Use controlled fixtures before changing behavior. A local subprocess observer
does not establish that an ended Codex turn wakes automatically. Real-use
observations find unexpected cases; they do not replace controlled comparison.
