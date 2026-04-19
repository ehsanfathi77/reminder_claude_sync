# docs/reference/

Reference material captured for the GTD skill design — external code and
skills we want to study or extend. Not live code.

| Path | What | Why |
|------|------|-----|
| `productivity-skills/` | Flat-layout copy of the installed `productivity` plugin (SKILL.md files + dashboard.html + mcp.json + README) | Foundation the GTD layer builds on |
| `productivity-plugin/` | Partial copy with the plugin's original layout (top-level files + `.claude-plugin/plugin.json`); `skills/` empty due to sandbox permission constraints | Kept so the `plugin.json` shape is visible |
| `gtd-refs/` (expected) | Clones of upstream GTD+Claude repos we want to study | See `../gtd-refs-clone.sh` — run locally on your Mac; the sandbox can't reach github.com |

See `../gtd-skill-design.md` for the design that ties all this together.
