---
name: reflect-on-development
description: Review recent Codex and Claude development threads, repository changes, validations, and operational failures; turn repeated or severe lessons into lean improvements to tests, scripts, docs, agent guidance, or skills; verify previous improvements; and identify stale context to consolidate or retire. Use for daily or weekly engineering reflection, after difficult debugging, or when development context and repeated mistakes need maintenance.
---

# Reflect on Development

Turn recent work into a small set of evidence-backed improvements. Prefer automation over prose for mechanical rules and accept an explicit no-change result.

## Workflow

1. Confirm Groundloop is initialised with `groundloop status --json`.
2. Set the review window: use the last successful reflection checkpoint, falling back to 36 hours for a daily review or seven days for a weekly review.
3. Create a privacy-filtered source packet with `groundloop digest --since-hours <hours>`. Never read `.env` files or dump settings, environment variables, headers, or credentials.
4. Correlate the digest with bounded repository evidence: commits since each checkpoint, changed tests/docs/scripts/skills, validation results, and existing guidance.
5. Run `groundloop lesson list --json` and review newly observed or active operational failures. Identify the correction only from safe evidence; do not retain complete commands.
6. Cluster candidates under architecture, code/contracts, tests/evals, operations, scripts/tooling, and collaboration/process.
7. Record a lesson only when it has a concrete symptom, impact, evidence count, confidence, and proposed destination.
8. Review prior proposals and decide `keep`, `adjust`, `consolidate`, `verify`, or `retire`.
9. Produce a self-contained HTML report in the reports directory returned by the digest command.
10. After the complete report exists, run `groundloop reflection-complete --report <path>`. Never advance the checkpoint for a partial review.

## Promotion rules

- Promote after two independent occurrences, or one severe incident with a verified cause and prevention.
- Put cross-project invariants in the developer's shared agent guidance and product truth in the owning repository documentation.
- Put conditional procedures in an existing skill. Create a new skill only when it has a distinct trigger and repeatable workflow.
- Put deterministic prevention in a test, preflight, linter, hook, or script.
- Keep ticket numbers, current branches, environment state, and one-off investigations in the dated report.
- Consolidate duplication into one source and replace copies with pointers.
- Propose retirement when guidance is superseded, contradicted, duplicated, unreferenced, or describes completed work. Never auto-delete source context.

## Operational lessons

Use `groundloop lesson observe` only when the automatic fingerprint lacks a safe, useful resolution. Keep the symptom generic and secret-free. A one-off typo stays in `watch`; repeated independent sessions move it to `active`. Resolve or retire it when prevention is implemented and verified.

Examples of appropriate durable outcomes:

- PowerShell interpolation failure → concise scripting reference or tested helper.
- Git command run at a non-repository workspace root → repository-discovery preflight.
- Evidence fixture bypassing a deployed contract → end-to-end regression test.

## Report contract

Lead with decisions, not a transcript summary. Include:

1. Review window, project checkpoints, source counts, and collection limitations.
2. At most five material lessons with category, evidence count, impact, and confidence.
3. A decision register using `promote`, `update`, `consolidate`, `automate`, `evaluate`, `purge`, or `keep`, with exact destination and rationale.
4. Applied changes and validation only when integration was authorised.
5. Purge candidates with replacement and risk; never delete automatically.
6. Regression or evaluation cases created or proposed.
7. At most three next actions.

Route unresolved, high-value exploration to `scout-development-improvements` instead of broadening the daily review.
