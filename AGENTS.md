# Agent instructions

Read and follow **[CLAUDE.md](CLAUDE.md)**: it is the single guide for every AI coding agent
(Claude, Codex, others) and for teammates working on this repo.

The short version:
1. Never push to `main`; use a branch and a Pull Request (`main` deploys straight to the live site).
2. Run `.venv/bin/python tests/run_checks.py` before pushing; it must print `ALL PASSED`.
3. Keep SQL working on both SQLite and Postgres, and never store files on disk.
4. Don't weaken the anti-cheat rules listed in CLAUDE.md.
