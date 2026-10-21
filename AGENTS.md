# SessionCrate

- Build a local-only, standard-library Python CLI for AI coding session archiving, content search, verification and restoration. Preserve the existing Codex CLI and v1 archives.
- Never inspect the real user's AI-tool sessions, credentials or configuration during development. Use temporary synthetic fixtures.
- Keep source transcripts byte-for-byte recoverable. Source deletion is explicit, follows archive verification, checks the source has not changed, and skips selected session files that are in use or whose activity cannot be checked. Unrelated Codex processes do not block cleanup.
- Use explicit per-tool source adapters. Codex rollout JSONL remains supported. Claude/Grok session companions must be archived as a complete group before any group removal. For opaque or shared-database tools use explicit plaintext exports, never guess private schemas. Do not mutate tool databases, indexes, config, authentication, worktrees or project files.
- Reject symlinks and unsafe paths. Store private archive files with restricted permissions. No network access at runtime.
- Search compressed files as streams; do not create a second plaintext transcript store. Support Korean and literal substring queries.
- Work concurrently with explicit file ownership. Preserve other agents' edits.
- The public command/package is `sessioncrate`. Keep legacy checkout launchers, the `codex_vault` module, archive identity salts/formats and the default `codex-vault` data directory compatible. Do not migrate user archives as part of branding changes.
- `reviews/` contains local development evidence and is excluded from version control and distributions. Public documentation must not include personal paths or real session records.
- Validate with `bash Scripts/verify.sh`; tests must use temporary directories and meaningful corruption, mutation, traversal and restore-conflict cases.
