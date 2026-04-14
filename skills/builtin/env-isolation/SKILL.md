---
name: env-isolation
description: Best practices for project-isolated environment setup. All installations must be local to the project, never global.
tags: [system, environment, isolation, conda, npm]
---

# Environment Isolation

## Core Principle

Every project runs in its own isolated environment. Installations must NEVER pollute the
global system or affect other projects.

## Before Installing Anything

1. Check if the tool already exists on the system:
   - `which <tool>` — is it on PATH?
   - `<tool> --version` — what version?
   - `ps aux | grep <service>` — is a daemon already running?
2. If it exists and the version is compatible, USE IT — do not reinstall.

## Python Packages

- Install into the project's conda environment (`.env/` directory)
- Use: `conda run --prefix .env pip install <package>`
- NEVER use bare `pip install` (installs to global site-packages)

## Node.js Packages

- Install locally in the project directory: `npm install <package>` (no `-g` flag)
- For CLI tools, use `npx <tool>` instead of global install
- NEVER use `npm install -g` — it pollutes the shared Node.js installation

## System Packages

- If a system package is truly needed (rare), document it in `results/needs_human.json`
  and request human intervention rather than running `sudo apt install`

## Verification

After installation, always verify:
- The package is importable / the CLI tool responds
- It is installed in the project-local path, not globally
