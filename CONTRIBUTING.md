# Contributing

Thanks for your interest in improving the Gemini License Provisioner.

## Ground rules

- Nothing in this repo should ever be tied to a specific GCP project or Workspace
  domain. Configuration is env-var / repository-variable driven — see
  `app/config.py` and `terraform/variables.tf` for the pattern to follow.
- Don't commit secrets, service-account keys, or `.tfvars`/`.tfstate` files —
  `.gitignore` already excludes the common cases, but double-check before pushing.

## Proposing a change

1. Open an issue first for anything non-trivial, so the approach can be discussed
   before you invest time in it.
2. Fork the repo (or, if you're a listed collaborator, branch directly) and make
   your change.
3. Follow the local dev/test setup in [`setup_instructions.md`](setup_instructions.md).
4. Add or update tests under `tests/` for any behavior change, and run:
   ```
   pip install -r requirements.txt
   pytest
   ```
5. Open a pull request against `main` with a clear description of the change and
   why it's needed. Every PR requires review from a code owner (see
   `.github/CODEOWNERS`) before it can be merged.

## Reporting bugs vs. security issues

Regular bugs: open a GitHub issue.

Security vulnerabilities (anything involving auth, license assignment, or admin
impersonation): see [`SECURITY.md`](SECURITY.md) instead of filing a public issue.
