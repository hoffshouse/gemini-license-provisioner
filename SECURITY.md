# Security Policy

This project provisions Google Workspace / Gemini Enterprise licenses using a
service account with Domain-Wide Delegation. Because it touches admin-level
Workspace access, please report suspected vulnerabilities privately rather than
opening a public issue.

## Reporting a vulnerability

Email **david@hoffshouse.com** with:

- A description of the issue and its potential impact.
- Steps to reproduce, or a proof of concept if you have one.
- Any relevant logs or configuration (with real project IDs, emails, or tokens
  redacted).

You should get an initial response within a few business days. Please give us a
reasonable amount of time to investigate and release a fix before disclosing
publicly.

## Scope

In scope: the application code (`app/`), the deploy workflow
(`.github/workflows/deploy.yml`), and the Terraform reference config
(`terraform/`). Out of scope: vulnerabilities in third-party dependencies without
a demonstrated impact on this project — report those upstream instead.
