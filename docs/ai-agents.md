---
icon: lucide/bot
---

# AI agents

django-dbos ships an [agent skill](https://agentskills.io/) for AI coding
agents: a `SKILL.md` file teaching them how to use django-dbos in *your*
project — declaring tasks, durable workflows and cron schedules, configuring
queues, testing and deploying. Once installed, the agent loads it on its own
whenever you work on background tasks.

The skill lives in the repository, under
[`skills/django-dbos/`](https://github.com/lewoudar/django-dbos/tree/main/skills/django-dbos).

## Installing with `npx skills`

The [`skills`](https://github.com/vercel-labs/skills) CLI installs it for
most agents (it needs Node.js):

```bash
npx skills add lewoudar/django-dbos --skill django-dbos
```

It asks which agents to install the skill for. By default the skill goes into
the project, so commit it and your whole team gets it; add `-g` to install it
for your user instead, in every project. Later, `npx skills update django-dbos`
brings it up to date with new django-dbos releases.

## Per agent

Pick your agent to target it directly, or to install the skill by hand:
either way, the `skills/django-dbos/` folder ends up in the agent's skills
directory.

=== "Claude Code"

    ```bash
    npx skills add lewoudar/django-dbos --skill django-dbos -a claude-code
    ```

    Or copy `skills/django-dbos/` into `.claude/skills/` (project) or
    `~/.claude/skills/` (user). Claude uses the skill when relevant; you can
    also invoke it with `/django-dbos`.

=== "Codex"

    ```bash
    npx skills add lewoudar/django-dbos --skill django-dbos -a codex
    ```

    Or copy `skills/django-dbos/` into `.agents/skills/` (project) or
    `~/.agents/skills/` (user).

=== "Cursor"

    ```bash
    npx skills add lewoudar/django-dbos --skill django-dbos -a cursor
    ```

    Or copy `skills/django-dbos/` into `.agents/skills/` or `.cursor/skills/`
    (project), `~/.agents/skills/` or `~/.cursor/skills/` (user).

=== "OpenCode"

    ```bash
    npx skills add lewoudar/django-dbos --skill django-dbos -a opencode
    ```

    Or copy `skills/django-dbos/` into `.agents/skills/` (project) or
    `~/.config/opencode/skills/` (user).

=== "Pi"

    ```bash
    npx skills add lewoudar/django-dbos --skill django-dbos -a pi
    ```

    Or copy `skills/django-dbos/` into `.agents/skills/` (project) or
    `~/.agents/skills/` (user).

!!! tip
    Codex, Cursor, OpenCode and Pi all read `.agents/skills/`: one copy there
    serves every one of them.

The `skills` CLI supports many more agents; see
[its list](https://github.com/vercel-labs/skills#supported-agents).
