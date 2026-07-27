# GitHub Required Settings

These controls cannot be enforced from repository code alone. They must be set
in GitHub repository settings by a maintainer with admin rights.

## Branch Protection

Protect `main` with:

- Require a pull request before merging
- Require at least one approval
- Dismiss stale approvals when new commits are pushed
- Require conversation resolution before merge
- Require status checks to pass before merge
- Restrict direct pushes to administrators only if your process allows it

## Required Status Checks

Set these checks as required after they exist in Actions:

- `test`
- `postgres-smoke`
- `migration-safety`
- `lint`
- `pip-audit`

## Merge Policy

Recommended:

- Allow squash merge
- Disable merge commits if you want linear release history
- Keep rebase merge optional

## Why This Exists

The repo can define CI, but it cannot force GitHub to block merges on red CI.
Without branch protection, a green workflow file is only advisory.
