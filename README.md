# Groundloop

Groundloop helps coding-agent sessions start from current project reality and turns repeated development friction into verified improvements. It uses a rebuildable JSON ledger; no service or database is required.

## Local alpha

```powershell
python -m pip install -e .
groundloop install --hosts codex,claude --codex-profile work
Set-Location C:\path\to\workspace
groundloop init
groundloop doctor
codex-ground-loop
```

Use `codex -p work` or plain `claude` as the baseline. Groundloop launchers preserve trailing arguments and enable the installed hooks only for their child process.

Groundloop keeps generated configuration, state, reports, and plugin cache under `~/.groundloop`. It does not modify a project unless `groundloop init --shared` is explicitly selected.

## Daily loop

1. Start with `codex-ground-loop` or `claude-ground-loop`; the concise status confirms repository drift, freshness, and active lessons.
2. Work normally. Failed-tool hooks retain only sanitised fingerprints, and only repeated active lessons are injected into relevant prompts.
3. Ask the agent to use `reflect-on-development` at the end of the day or after a difficult thread. It reviews the bounded digest, code changes, validations, lessons, and stale guidance, then checkpoints a self-contained HTML report.
4. Use `scout-development-improvements` separately when reflection exposes a high-value unknown. It proposes one reversible experiment; it does not install or change anything without approval.

Run `groundloop compare` after enough similar baseline and treatment sessions to inspect directional evidence. Do not treat small unmatched cohorts as a performance claim.

## Commands

- `groundloop init`: discover bounded repositories and initialise private project state.
- `groundloop status`: print the concise session summary; add `--json` for machine output.
- `groundloop refresh`: fetch configured baseline refs outside the startup hook.
- `groundloop digest`: create a privacy-filtered recent-thread digest.
- `groundloop compare`: compare unmarked and Groundloop treatment sessions as directional evidence.
- `groundloop reflection-complete`: checkpoint a completed reflection report.
- `groundloop lesson`: observe, list, resolve, or retire operational lessons.
- `groundloop install`, `doctor`, `uninstall`: manage host integration.
- `groundloop update`: refresh host integration with a Codex cachebuster so new hooks and skills load in new sessions.

## Trust and privacy

Groundloop never reads `.env` files. Thread digests contain user text, final assistant responses, and concise failed-tool excerpts with pattern-based redaction. Hook state contains sanitised failure fingerprints, not complete commands or successful output.
