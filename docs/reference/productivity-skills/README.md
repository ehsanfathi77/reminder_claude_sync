# Productivity plugin — reference copy

Snapshot of the `productivity` plugin's contents as they existed in the installed
plugin bundle on 2026-04-19. Saved here so we can reference the exact shape of
the skills, commands, and MCP server list while building the GTD layer on top.

**This is not the live copy.** Do not edit these files expecting plugin behaviour
to change. The live plugin lives under:

```
~/Library/Application Support/Claude/.../rpm/plugin_01MKcJsEAmPJswuCytbMJYZJ/
```

## Manifest

| File | Role | Notes |
|------|------|-------|
| `memory-management.SKILL.md` | Helper skill (not user-invocable) | Two-tier memory: `CLAUDE.md` hot cache + `memory/` deep store |
| `task-management.SKILL.md` | Helper skill (not user-invocable) | Canonical shape of `TASKS.md` (Active / Waiting On / Someday / Done) |
| `start.SKILL.md` | `/productivity:start` | First-run bootstrap — creates `TASKS.md`, `CLAUDE.md`, `memory/`, `dashboard.html` |
| `update.SKILL.md` | `/productivity:update` | Sync tasks + fill memory gaps; `--comprehensive` scans chat/email/calendar/docs |
| `dashboard.html` | Visual UI | Single-file HTML that reads/writes `TASKS.md` and watches for external changes |
| `mcp.json` | MCP server list | HTTP-only MCPs shipped with the plugin (slack, notion, asana, linear, atlassian, ms365, monday, clickup, gcal, gmail) |

Original in-plugin layout (for reference when cross-checking):

```
productivity/
├── .claude-plugin/plugin.json
├── .mcp.json
├── README.md
├── CONNECTORS.md
├── LICENSE
└── skills/
    ├── dashboard.html
    ├── memory-management/SKILL.md
    ├── task-management/SKILL.md
    ├── start/SKILL.md
    └── update/SKILL.md
```

Filenames here are flattened (`<skill>.SKILL.md`, `mcp.json`) because the
sandbox wouldn't let us recreate nested `skills/<name>/SKILL.md` subdirectories
under `productivity-plugin/`.

## Why we care

The GTD skill we're building is a layer on top of this plugin:

- `TASKS.md` is the existing interface — GTD will migrate to a richer
  per-list Apple Reminders layout but keep `TASKS.md` as a derived view.
- `CLAUDE.md` + `memory/` stay exactly as specified in `memory-management`.
- `/productivity:update` is the closest analogue to the GTD "engine tick"
  that we'll extend.

See `../../gtd-skill-design.md` for the full design doc.
