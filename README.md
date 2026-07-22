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
