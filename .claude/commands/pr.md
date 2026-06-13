# Create a pull request

Create a PR for the current branch following the project conventions.

## Steps

1. Ensure you're not on `main` — if you are, ask the user to name a branch first.
2. Run `git status` and `git diff main...HEAD` to understand what's changed.
3. Push the branch if not already pushed: `git push -u origin HEAD`
4. Create the PR with `gh pr create` using this format:

```
gh pr create --title "<concise title under 60 chars>" --body "$(cat <<'EOF'
## What

<1-3 bullets describing the change>

## Why

<motivation — what problem this solves or feature it adds>

## Test

- [ ] Menu bar app starts with exactly one instance
- [ ] `ccusage` CLI shows correct output
- [ ] Live plan data fetches successfully (requires Firefox + claude.ai session)
EOF
)"
```

5. Output the PR URL so the user can review it.

## Rules

- Title should be imperative mood: "Add X", "Fix Y", "Update Z"
- Never push directly to `main`
- If tests or checks fail, fix them before asking to merge
