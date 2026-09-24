# Persistent project setup

Use when the user asks to apply Engineering Agents permanently to a project.

1. Locate the intended project root. Inspect existing `AGENTS.md` files, repository
   state, manifests and CI. Do not initialize inside the Engineering Agents source
   repository itself or infer a target from an unrelated working directory.
2. Run `python3 <this-skill>/scripts/install.py --project <project-root>`. The installer
   copies this version to `.agents/skills/engineering` and appends a small marked
   block to the root `AGENTS.md`. If this is already the project copy, the operation
   is idempotent. Do not use `--update` unless updating the setup was requested.
3. On a conflict, inspect the differences. Preserve local customization; do not
   delete an existing skill folder or overwrite project instructions to bypass
   the conflict. Complete independent project analysis while resolving it.
4. Create or update `docs/engineering-context.md`, preserving existing facts. Record
   only useful project-specific information: product goal, observed stack,
   architecture boundaries, actual setup/build/test commands and important
   constraints. Mark unknowns as unknown; don't invent test commands. Link this
   context from `AGENTS.md` outside the installer's marked block.
5. Adapt the workflow to project scale. Confirm that `AGENTS.md` points to the local
   skill and that all role references are present. State the installed version,
   commands verified and any open decisions. Commit/push these files when the
   user's task authorizes it; installation alone is not publication authority.

For an empty project, capture the goal and unresolved stack decisions before
generating application scaffolding. Setup does not mandate creating an application.

The checked-in copy enables colleagues or a compatible Codex cloud environment to
read the same workflow. It does not install Codex, provision credentials, configure
network access, enable GitHub automatic review, or make unavailable tools available.
