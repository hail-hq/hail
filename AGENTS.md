# Working rules for agents

## Worktrees and pull requests

- Always implement changes in a separate Git worktree. Keep shared checkouts and other sessions untouched.
- Make every commit and push on a feature branch. Never commit to `main`.
- Deliver implementation through a pushed pull request. Local-only changes are not a completed handoff.
- Explain the changes, material choices, validation, and review needs. Keep the handoff concise.
- Include the full URL whenever you mention a pull request.
- After a pull request merges, remove its temporary worktree and delete the merged local branch. Check `/Users/r/playground/hail*` for other merged worktrees too.
- Keep worktrees whose pull requests are open or closed without merging. Keep any worktree with uncommitted changes. Report retained worktrees.
- Do not add `Co-Authored-By` lines or AI attribution to commits or pull requests.
- Stage each file separately. For a new file, use `git add -N`, then `git add -p`. For a tracked file, use `git add -p`.

## Pricing ownership

- Public provider costs belong in `costs/`. Private Hail Cloud retail rates belong in the `hail-website` repository.
