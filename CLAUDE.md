# ms-a2-fancontrol – rules for contributors and AI assistants

This repository is **public**. Everything committed here – code, docs, commit messages, branch
names, issues, PR descriptions and release notes – is visible to everyone and stays in the git
history.

## Never commit or publish

- Secrets of any kind: passwords, API keys, tokens, private keys, certificates, `.env` files.
- Data about real machines or networks: hostnames, IP addresses (including private ranges such
  as `192.168.x.x`), MAC or serial numbers, SSH host names or aliases, key file paths.
- Personal data: real names other than the git author, e-mail addresses, local user paths
  (`C:\Users\...`, `/home/<name>/...`).
- Build and tool output: `__pycache__/`, `*.pyc`, `dist/`, `*.deb`, `.claude/`.

Use neutral placeholders in examples (`node1`, `<host>`, `192.0.2.10` from the documentation
range RFC 5737).

## Safeguards

- `.gitignore` excludes build output, tool state and common key/secret file names.
- `.github/workflows/secret-scan.yml` runs gitleaks (default rules plus `.gitleaks.toml`) on the
  full history of every pushed branch and every pull request.
- GitHub secret scanning with push protection is enabled for the repository.

If something slipped through: do not just delete it in a new commit. Rotate the secret first,
then rewrite the history and force-push only after the maintainer agrees.

## Before every commit

- Check `git diff --cached` for the items above.
- Stage files by name, not with `git add -A` / `git add .`.
