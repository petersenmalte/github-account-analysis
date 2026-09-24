---
name: engineering
description: Apply the Engineering Agents workflow to a software project, including project setup, architecture, implementation and independent PR review. Use when the user invokes this setup or the project's AGENTS.md selects it.
---

# Engineering

Act as the main agent responsible for the requested software outcome. Use the
project's conventions and the user's scope. This skill defines a workflow, not a
runtime or a model: inherit the current Codex model settings unless the user or
applicable configuration specifies otherwise.

## Select the operation

- **Set up a project permanently:** read [project setup](references/project-setup.md).
- **Implement or fix:** follow the workflow below.
- **Review only:** read [review](references/review.md); return findings without
  implementing changes unless the user asks for fixes.

If `AGENTS.md` selects a project-local copy of this skill, use that exact copy and
its references throughout the task. Do not combine different installed versions.

## Development workflow

1. Inspect applicable project instructions, relevant code, working-tree changes,
   and available test commands. State the intended outcome and derive observable
   acceptance criteria. Ask only about missing decisions that materially affect it.
2. For a new component or a consequential change to interfaces, storage, boundaries
   or dependencies, use [architecture](references/architecture.md). Keep small
   fixes lightweight; do not invent an architecture phase for every edit.
3. Implement a coherent change and appropriate tests. Preserve unrelated work.
   Run the project's relevant checks and distinguish executed checks from proposed
   ones. Delegate implementation only when ownership can be separated cleanly.
4. For substantive code changes, explicitly delegate an independent review using
   [review](references/review.md). Give the reviewer the actual diff/files and
   acceptance criteria; require it to inspect evidence rather than endorse your
   account. Trivial documentation or formatting edits may use a direct check.
5. Investigate and fix supported findings. Rerun affected checks after changes;
   obtain a focused follow-up review for material fixes. If two review/fix rounds
   still leave the same unresolved issue, report the blocker instead of looping.
6. Deliver the integrated outcome, checks, remaining limitations and PR link if
   applicable. Use existing GitHub tools for authorized branch, push and PR work.
   This skill itself does not authorize merge, release, deployment or messages.

## Delegation contract

This skill requests subagent delegation when the criteria above apply. Use the
host's subagent tools, not separate user-owned chats. The main agent owns final
integration, requirements and communication.

Each assignment includes: objective, relevant acceptance criteria, exact paths or
diff base, role guidance, read/write scope, expected result, and how it can be
verified. Reference paths are relative to this skill; pass resolved paths or the
relevant guidance to agents that cannot access them.

- Architecture and review agents are read-only unless explicitly assigned edits.
- A development subagent owns a bounded change and its tests. It must report files
  changed, checks run and unresolved assumptions. Avoid overlapping writers; use
  separate ownership or worktrees when actual isolation is needed.
- For high-risk behavior or substantial integration uncertainty, delegate a
  test/QA pass using [testing](references/testing.md). Ordinary tests remain the
  implementing agent's responsibility.
- Start only agents that have useful, bounded work. Respect host concurrency limits
  and wait for required results before declaring completion.
- If delegation is unavailable, continue sequentially and explicitly disclose that
  the review was a self-review. Do not claim independent validation.

## Scope and completion

Maintain requirements in the main task; introduce a separate requirements role only
when stakeholder or backlog complexity warrants it. Persist accepted decisions in
existing project documentation when useful. Do not prescribe a language, framework,
test suite, branch naming policy or mandatory approvals absent project requirements.

A completed implementation meets its acceptance criteria, has relevant checks
reported with evidence, and has no known unresolved blocking review findings.
When environment limits prevent verification, describe what remains unverified.
