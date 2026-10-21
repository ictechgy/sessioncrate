# Multi-tool extension contract

User request: 다른 ai툴들도 가능하게 해줘 claude agy grok glm 등등

Keep the existing Codex v1 backend and installed command/skill. Add a separate grouped-source backend sharing the existing storage safety primitives. No personal data is used in development.

## Source adapters (`codex_vault/sources.py`)

Public interface:

```python
@dataclass
class SessionGroup:
    tool: str
    source_root: Path
    session_id: str
    files: tuple[str, ...]         # canonical relative regular files
    root_entries: tuple[str, ...]  # same-parent selected file/dir roots for quarantine
    formats: dict[str, str]        # each relative file -> jsonl/json/text/binary parser name
    cwd: str = ''
    source_mode: str = 'native'    # native or export
    removable: bool = True
    notes: tuple[str, ...] = ()
```

- `tool_catalog() -> list[dict]`: IDs, labels, mode, default root (or null), caveat.
- `default_source_root(tool: str) -> Path | None`.
- `discover(tool: str, source_root: Path) -> list[SessionGroup]`: metadata only; never read transcripts or configs. Recognized session paths only. Return unsafe/incomplete groups as `removable=False` with notes, or reject safely; never silently exclude a companion then allow whole-directory deletion.
- `valid_relative(tool: str, path: str, source_mode: str) -> bool`: lexical canonical path, allowed source scope and no credential/config filenames. Metadata integrity validation uses this too. Whole native source roots themselves are never candidates.
- `iter_text_records(stream, format: str, max_record_bytes=1048576) -> Iterator[dict]`: yields `line`, `kind`, `text` or `skipped=True` reason. Bounded JSONL lines; bound whole JSON at 32 MiB and skip/report larger JSON while raw archive still round-trips. Ignore image/base64/encrypted payloads; control-safe text. Unknown schemas fail explicitly or emit skipped, not invented successful text.
- `metadata_from_bytes(data: bytes, format: str) -> dict`: best-effort bounded metadata from already-read snapshot prefix; no filesystem access. Return session_id/cwd/tool_version/started_at when established.

Target adapters:

- Claude Code native: `~/.claude` or `CLAUDE_CONFIG_DIR`; group `projects/<project>/<sid>.jsonl` plus every regular artifact under sibling `<sid>/` (subagents, tool-results, sidecars). Group safety checks all artifacts. No Claude Desktop/Cowork.
- Grok Build native: `$GROK_HOME` or `~/.grok`; each `sessions/<encoded-cwd>/<sid>/` is a group. Preserve full session directory, never global config/auth/search index. `updates.jsonl` ACP events are searchable; known text/json companions may also be searched.
- Gemini CLI native if official current source confirms `tmp/<project>/chats/session-*.json`; otherwise explicit export only. Root `~/.gemini`, no Antigravity directories selected by Gemini.
- `opencode`, `glm`: explicit export root REQUIRED. OpenCode `{info,messages:[{info,parts}]}` JSON exports plus documented textual chat exports. Native shared DB is untouched. GLM source label is user-selected export origin, not proof of model identity.
- `agy`: explicit export root REQUIRED. Only user-provided plaintext session exports (JSON/JSONL/Markdown/text); native Antigravity SQLite is unsupported, no guessed paths/schema.
- `export`: same explicit plaintext export adapter for other tools.

## Grouped backend (`codex_vault/bundles.py`)

Uses `storage.VaultError` and private safety primitives, not the legacy Codex manifest namespace. Source adapter errors become VaultError without transcript text.

- Store under `<vault>/bundles/blobs/<compressed-sha256>.tar.gz` and `<vault>/bundles/manifests/<archive-id>.json`. Files private, atomic, fsynced, protected by the existing vault writer lock.
- Manifest contains strict schema version, tool, source_root, session_id, cwd, source_mode, root_entries, files (path/sha256/size/mode/mtime_ns/format), compressed blob path/hash, raw_bytes/compressed_bytes/archived_at. Bind archive ID to canonical manifest identity, including source paths and per-member hashes; never weaken checks when originals disappear.
- Tar is only regular files, exact relative names, no symlinks/hardlinks/device entries/duplicate members or traversal; validate compressed digest AND every member hash/size/count before claiming integrity. Never use `extractall`.
- Snapshot must be stable before/after archive; fully verify/publish bundle+manifest before removal.
- Source removal is explicit. Protect recent groups (<1 hour), changed/incomplete/unsafe groups, open handles or ambiguous activity. Check all source group roots. Stage all selected same-parent root entries into a private random sibling quarantine, use `activity.probe_session_tree(quarantine)` after rename, then verify exact member set and all hashes/identity before removing only expected paths. New files created at original locations are never removed. Restore root entries on activity-block when possible without overwrite; otherwise preserve quarantine and report recovery. No global Codex shutdown condition.
- Existing destination files are never overwritten with different bytes. Verify the entire bundle and preflight all destination conflicts before writing. Restore bytes/mode/mtime and preserve existing directory permissions.
- Search tar.gz streams using `sources.iter_text_records`, return member path and line plus bounded matching snippet. No plaintext index/cache. Korean literal substring matching and all-tool filters.

API (JSON-safe results, same conventions as legacy):

```python
scan(tool, source_root, older_than_days=30)
archive(tool, source_root, vault_dir, older_than_days=30, execute=False, remove_originals=False)
list_archives(vault_dir, tool=None)
verify(vault_dir, archive_id=None, tool=None)
restore(vault_dir, archive_id, destination_home=None, execute=False)
prune(vault_dir, archive_id=None, execute=False, tool=None)
search(vault_dir, query, limit=20, project=None, session_id=None, tool=None)
show(vault_dir, archive_id, limit=100, query=None)
stats(vault_dir)
```

`archive` returns `archived`, `removed`, `skipped`; `list` items include `archive_id/tool/session_id/cwd/raw_bytes/compressed_bytes/source_mode`; search returns `matches/scanned_archives/truncated/skipped_records`. Every mutation defaults to preview.

## CLI ownership (root)

Add `tools`, `--tool`, `--source-root`; preserve `--codex-home`. Native roots have defaults; export tools require explicit source root. `scan/archive` default Codex; `list/search` default all tools; `prune` default Codex unless an archive ID or explicit tool selects another scope. Route archive IDs without reading personal sources. Merge catalog/search/stats without dropping v1 archives.

## Verification

Meaningful TDD and synthetic fixtures only. Preserve the existing Codex tests. Add native Claude companion and Grok full-directory roundtrip/search; export modes AGY/GLM; unknown native DB never selected; legacy coexistence; malicious tar/manifest/path rejection; corrupt/changed/active/ambiguous group preserved; no restore overwrite; current CLI/skill installation remains functional. Native app resume remains unverified and must be described honestly.
