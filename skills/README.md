# pool skill for AI coding agents

[`pool/`](pool) is an [Agent Skill](https://agentskills.io/specification).
It teaches an AI coding agent (Claude Code, Codex, Gemini CLI and others that
read `SKILL.md`) how to use pool properly:

- run a program on the group's computers,
- follow the container rules,
- wait for the job without cancelling it by accident,
- collect the files and the results table,
- fix the common errors.

## What is in it

| File | What it holds | When the agent reads it |
|---|---|---|
| `pool/SKILL.md` | When to use pool, the rules, the steps, the sizes, exit codes, what not to do | When a task matches the skill |
| `pool/references/job-files.md` | Job files: parts, sweeps, placeholders, examples | For many runs of one program |
| `pool/references/dockerfiles.md` | Dockerfile patterns for Python, Node, Java, Go, C++, R, Rust, .NET | When it writes a Dockerfile |
| `pool/references/troubleshooting.md` | Each error message and its fix | When something fails |
| `pool/scripts/check.sh` | Checks the pool command, the sign-in and Docker | Before the first run |
| `pool/scripts/wait.sh` | Waits for a job to end, safely | After `pool run -d` or `pool submit` |

## Install it

Copy (or link) the `pool` folder into the folder your agent reads skills from.
A link keeps it up to date when you pull this repository.

| Agent | For you (all projects) | For one project |
|---|---|---|
| Claude Code | `~/.claude/skills/pool` | `.claude/skills/pool` |
| Codex | `~/.agents/skills/pool` | `.agents/skills/pool` |
| Gemini CLI | `~/.gemini/skills/pool` or `~/.agents/skills/pool` | `.gemini/skills/pool` or `.agents/skills/pool` |

For example, from the root of this repository:

```bash
mkdir -p ~/.claude/skills && ln -s "$PWD/skills/pool" ~/.claude/skills/pool   # Claude Code
mkdir -p ~/.agents/skills && ln -s "$PWD/skills/pool" ~/.agents/skills/pool   # Codex and Gemini CLI
```

The folders above come from each tool's documentation, read on 6 November 2026:

- [Claude Code](https://code.claude.com/docs/en/skills)
- [Codex](https://developers.openai.com/codex/skills)
- [Gemini CLI](https://geminicli.com/docs/cli/skills/)

Check there if they have changed. The Gemini CLI page also says that Gemini
CLI was replaced by Antigravity CLI for some users on 18 June 2026.

## Before the agent uses it

1. Sign in once yourself: `pool login --server URL --token TOKEN --group GROUP`.
   The agent never asks for your token.
2. Until the `pool` command is packaged, tell the scripts where it is:
   `export POOL_CMD="uv run --project /path/to/pool pool"`.
3. Ask the agent to run `scripts/check.sh` from the skill. It prints what is missing.

## What the agent is told not to do

- put secrets in a job,
- cancel or change other people's jobs or the group's settings,
- share your computer,
- submit a failing job again without reading its log.

It also asks you before a big run: many parts, GPUs, or more than an hour.
