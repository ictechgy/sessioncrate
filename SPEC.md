# SessionCrate v0.1 — legacy archive contract

Historical v1 implementation contract. The public project is now [SessionCrate](README.md); the legacy archive formats and identities described here remain compatible.

## User request

> 바탕화면에 폴더 하나 만들고 만들어줘. 일단 codex에서 쓸 수 있게. 아카이빙해서 용량도 줄일 수 있게 하고 아카이빙된거에 뭐가 들어갔는지 검색도 가능하게

## Scope

The multi-tool extension is specified in [MULTITOOL_PLAN.md](MULTITOOL_PLAN.md). Codex v1 archives and commands remain readable. Claude Code, Grok Build and Gemini CLI use native source adapters; AGY, GLM, OpenCode and generic tools use explicit plaintext exports when a supported native backup contract is unavailable. Provider names do not imply a shared native storage format.

- A Python 3.10+ CLI, zero third-party runtime dependencies, runnable from this checkout.
- Metadata-only scan of Codex rollout JSONL files below an explicit Codex home (default `$CODEX_HOME`, otherwise `~/.codex`).
- Per-file gzip archives named by SHA-256, with durable JSON manifests. Byte-identical content shares one compressed blob. Original relative paths and source roots remain in manifests for restoration.
- Archive previews are read-only. Executing an archive keeps originals unless removal is separately explicit. Original removal requires a verified blob, unchanged source and a selected-session file activity check. Open or unverifiable files are skipped individually; unrelated Codex processes do not block inactive files. Check both the original path and the quarantined inode before final verification/removal.
- Archive/list/stats, literal case-insensitive content search with Korean support, show, verify and restore commands. Search reads gzip streams and stores no plaintext search index.
- Full source bytes round-trip exactly. Restore never overwrites an existing different file; caller can choose an alternate Codex home.
- Private files, no network or telemetry. Do not read authentication/config files or modify any Codex-owned database/index.
- A local Codex skill and launcher installation that does not overwrite existing unrelated files.

## Acceptance

1. A synthetic Codex transcript can be archived, its content searched after its original is absent, verified and restored byte-for-byte.
2. Stats include compressed blobs and manifests and report actual net storage cost, not just moved source bytes.
3. A repetitive synthetic corpus demonstrates real compression, without claiming a universal compression ratio.
4. Truncated/corrupt blobs, mutated sources, unsafe paths/symlinks, concurrent vault writers and restore conflicts fail without loss of the original or an existing destination. Mixed batches preserve in-use/unverifiable session files while proceeding with eligible inactive files.
5. All essential commands work in a subprocess from outside the checkout, including paths containing spaces.

## Boundaries

- Installation and development do not archive or delete the user's real sessions.
- Restoring bytes is verified using fixtures; native Codex resume and desktop database behavior require a user-authorized live check later.
- No AI summarization, semantic search, token reduction, cloud sync, arbitrary attachments/worktrees cleanup or background deletion. Native Claude/Grok session companions are included only inside their selected complete session groups.
- Search is streaming and linear in the selected archive bytes. Metadata filters narrow the archives selected; result limits bound returned text while each opened archive is verified through its end. A compact optional index can be evaluated later against its storage cost.

## Compatible source extensions

Codex `rollout-*.jsonl.zst` files use the same v1 ID salt and outer gzip blob layout. The SHA-256 and `raw_bytes` describe the original Zstandard file bytes, and restore writes those bytes back to the original `.jsonl.zst` path. The outer gzip uses level 0 because the source is already compressed. These archives add a positive `decoded_bytes` field for search consistency; existing plain JSONL manifests need no new fields.

New Zstandard archiving and content search use Python 3.14's optional standard-library decoder. Metadata-only scan, archive byte verification, restoration, and guarded original removal remain available on older supported Python versions. Decoding is limited to a 128 MiB window and `min(8 GiB, max(64 MiB, source_bytes × 256))` total output. Search retains the 1 MiB record limit. The raw Zstandard line iterator additionally refuses physical lines over 16 MiB when no caller-supplied record bound is present.

Zcode and AutoClaw use a distinct `diagnostic` bundle source mode with an explicit flat log directory. A bundle contains one regular `model-io-[A-Za-z0-9_-]{1,80}.jsonl` file, no directories, and the dedicated `model-io-jsonl` reader. Its log-file ID is the filename stem. Capturing these rotated request-response logs does not include canonical session databases or establish native application resume support.
