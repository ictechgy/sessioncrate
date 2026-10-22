# SessionCrate

**Pack, search, and restore local AI coding sessions.**

SessionCrate is a local CLI for reducing the disk space used by old coding-agent transcripts while keeping them searchable. It creates verified compressed archives, searches readable conversation and tool output without a plaintext search index, and restores the original bytes when needed.

It uses Python's standard library, makes no model calls, and sends no data over the network. It is early alpha software: archive and restore integrity are tested with synthetic records, while native application listing, sidebar, and resume behavior is not certified. Read [compatibility and evidence](docs/compatibility.md) before removing originals.

[한국어 안내](README.ko.md) · [CI](https://github.com/ictechgy/sessioncrate/actions/workflows/tests.yml) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md)

## Quick start

Clone the repository, install it in a dedicated virtual environment, and inspect the supported sources:

```sh
git clone https://github.com/ictechgy/sessioncrate.git
cd sessioncrate
python3 -m venv .venv
.venv/bin/python -m pip install .
. .venv/bin/activate
sessioncrate tools
```

If you are running from an extracted checkout before installation, use `python3 bin/sessioncrate` instead. Python 3.10 or later is required. `lsof` is needed only when removing originals; compression, search, verification, and restoration do not require it. The package has no third-party Python runtime dependencies and is not yet published to PyPI.

For Codex `.jsonl.zst`, use Python 3.14 or later to create archives, search, or show content. Metadata scans and byte-exact verification, restoration, and guarded removal of already archived originals also work on older supported Python versions.

Start with a disposable fixture before selecting personal records:

```sh
python Scripts/demo.py
python Scripts/roundtrip_check.py --seed 12345  # optional longer check; requires lsof
```

The demo covers compression, search after the source is removed, and exact restoration. The roundtrip check creates disposable sessions for the supported adapter modes and exercises the CLI, `lsof`, original cleanup, alternate-location restore, hashes, permissions, timestamps, empty directories, and repeat operations. It never reads your personal histories.

## Archive, then reclaim space

Preview candidates first. `archive` is preview-only unless `--execute` is present, and an executed archive keeps the originals by default:

```sh
sessioncrate scan --tool codex --older-than 30
sessioncrate archive --tool codex --older-than 30
sessioncrate archive --tool codex --older-than 30 --execute
sessioncrate stats --json
sessioncrate stats --tool claude --json  # only Claude archives and their retained originals
```

Keeping the originals adds archive storage; space is reclaimed only after verified originals are removed. `stats` counts the archives, metadata, and retained originals. Preview cleanup, then opt in explicitly:

```sh
sessioncrate prune --tool codex
sessioncrate prune --tool codex --execute
```

`archive --execute --remove-originals` combines those operations. Originals are removed only after the integrity, source, and activity checks pass. Selected recent, changed, open, unsafe, or unverifiable originals are preserved. Other sessions may remain running; `lsof` checks open file descriptors, not an application's logical session state. Do not reopen the selected session during cleanup. See [compatibility and evidence](docs/compatibility.md) for native resume limits and [release preparation](docs/release.md) for the alpha boundary.

For grouped sessions, age checks include every selected file and directory, including empty companion directories. For bundle adapters such as Claude, `stats --tool TOOL` counts the selected manifests, their unique referenced blobs, and their retained originals; unfiltered `stats` includes all archive blobs, including unreferenced blobs. A blob shared across tool labels is counted once in each relevant tool view and once in the overall view.

## Supported sources

```sh
sessioncrate tools
```

| `--tool` | Input | Selected scope |
| --- | --- | --- |
| `codex` | Codex rollout JSONL, plain or `.jsonl.zst` | `sessions/` and `archived_sessions/` rollout files; compressed rollout source bytes are preserved exactly |
| `claude` | Native Claude Code CLI transcripts | Project main JSONL and same-session companions, including subagents and tool results |
| `grok` | Native Grok Build session directories | Complete `sessions/<project>/<session>/` groups; ACP parsing is best effort |
| `gemini` | Native Gemini CLI chat JSON | `tmp/<project>/chats/session-*.json` |
| `zcode` | Explicit Zcode model-I/O diagnostic logs | Direct regular `model-io-*.jsonl` files in one selected `rollout` or `debug` directory |
| `autoclaw` | Explicit model-I/O diagnostic logs | The same flat Zcode diagnostic layout may be selected; the label does not discover an app home |
| `agy` | Explicit plaintext exports | JSON, JSONL, Markdown, and text; native Antigravity SQLite is unsupported |
| `glm` | Explicit plaintext exports | A user-supplied origin label; it is not a native storage format |
| `opencode` | Explicit exports | OpenCode JSON/text exports; shared native databases are untouched |
| `export` | Explicit plaintext exports | Other user-provided conversation exports |

Native adapters use their known default roots and also accept `--source-root`. Export adapters require a dedicated directory containing only the exports you intend to archive:

```sh
sessioncrate archive --tool claude --older-than 30 --execute
sessioncrate archive --tool grok --older-than 30 --execute
sessioncrate archive --tool gemini --older-than 30 --execute
sessioncrate archive --tool glm --source-root /path/to/session-exports --execute
sessioncrate archive --tool zcode --source-root /path/to/.zcode/cli/rollout --execute
```

Export adapters require a dedicated export directory. Home, native account, database, and configuration directories are rejected when recognized; hidden paths and known credential/configuration files are excluded. Export cleanup reclaims only the exported files, so it does not shrink the originating application's database. Claude Desktop and Cowork are outside the native Claude adapter's scope.

Diagnostic adapters require an explicitly selected flat log directory. They select only direct regular `model-io-*.jsonl` files and leave subdirectories, databases, indexes, and other files outside the archive. Both `zcode` and `autoclaw` accept `.../.zcode/cli/rollout` or `.../.zcode/cli/debug`; neither discovers an app home automatically.

Zcode and AutoClaw files are request-response diagnostic logs. A log can contain deltas, resets, or truncated history, so the archive is an exact log-byte backup rather than a canonical session or application-resume format. Search extracts request `messages`, `sdkMessages`, or `body.messages`, response text and tool output, and `error.message`; headers, provider options, and reasoning fields are intentionally not searched. Raw archived logs remain private vault data and may contain secrets; SessionCrate does not promise universal redaction.

`scan` and `archive` default to Codex. `list`, `search`, and `verify` inspect all archive namespaces by default; an ID-free `prune` targets Codex unless you provide `--tool`.

## Search archived content

```sh
sessioncrate list
sessioncrate search "결제 재시도" --tool claude
sessioncrate search "payment.retry()" --project my-app --limit 10
sessioncrate search "migration" --session ARCHIVE_ID
sessioncrate show ARCHIVE_ID --limit 30
```

Search performs literal, Unicode case-insensitive substring matching. It streams compressed archive members and does not create a second plaintext transcript store. Results include the archive ID, project, source member or record location, and bounded context. Use project and session filters to narrow the archives searched. `--limit` bounds the number of returned results, not the verification work: each archive read is checked through its end, even after enough matches are found.

Unknown, binary, image, encrypted, or oversized records may be skipped by the text reader while their original bytes remain archived. JSONL records over 1 MiB and JSON documents over 32 MiB are skipped by the relevant readers; inspect `skipped_records` when an empty result matters. For JSON documents, `line` is a message ordinal rather than a physical file line.

For Codex `.jsonl.zst`, the original compressed source bytes are hashed and retained byte-for-byte inside the vault. The archive records the decoded byte count only to validate streaming search; it does not keep a second plaintext copy and does not promise additional compression savings. Searching or showing that content requires Python 3.14 or later. On older supported versions, byte-preserving scan, listing, statistics, verification, restoration, and pruning remain available, with a clear capability error for operations that need to decode the Zstandard content.

## Verify and restore

```sh
sessioncrate verify
sessioncrate verify ARCHIVE_ID
sessioncrate restore ARCHIVE_ID                    # preview
sessioncrate restore ARCHIVE_ID \
  --destination-home /path/to/restored --execute
sessioncrate restore ARCHIVE_ID --execute          # original recorded location
```

A unique archive-ID prefix is accepted. SHA-256 and compressed/member integrity checks complete before an archive is accepted for restoration. Restore refuses existing files with different bytes and leaves equal files in place. Original paths, bytes, permissions, and modification times are recorded where the source format supports them.

`list` reports valid entries alongside manifest errors, and `verify` aggregates failures. If one archive namespace cannot be searched, `search` returns healthy namespaces' matches with `partial: true` and `errors`; human output also identifies the failed namespace. An unrelated damaged manifest does not block full-ID recovery of an intact archive. A damaged matching prefix requires the full intact ID or an explicit tool scope. Integrity and operational errors, including partial searches, return exit code `2`.

Grouped restore uses a private staging directory near the destination. A crash can leave a `.codex-vault-restore-stage-*` plaintext staging directory; there is no automatic crash scavenger. Multiple session roots are not published as one filesystem transaction. Compression is not encryption.

## Codex skill and compatibility

From a source checkout, install the local command and Codex skill links:

```sh
python3 Scripts/install.py                    # preview
python3 Scripts/install.py --execute
python3 Scripts/install.py --legacy-aliases --execute  # optional old-name links
```

The primary targets are `~/.local/bin/sessioncrate` and `~/.agents/skills/sessioncrate`. In Codex, invoke `$sessioncrate`. The installer refuses to overwrite another installation and does not modify shell or Codex configuration. Checkout links require the checkout to remain in place.

The old checkout launchers and installed `codex-vault` aliases remain available for compatibility. The internal `codex_vault` module, v1 archive identities and formats, `--codex-home` option, and default vault location are unchanged:

- `$XDG_DATA_HOME/codex-vault`, or `~/.local/share/codex-vault`.
- `--vault PATH` selects an explicit archive directory.
- Branding changes do not relocate archives or migrate databases and indexes.

## Development

```sh
bash Scripts/verify.sh
python Scripts/roundtrip_check.py
```

Tests use temporary synthetic records. Do not use personal histories, account configuration, or authentication material as fixtures or attach them to an issue. See [compatibility and evidence](docs/compatibility.md) for the supported-platform matrix and the boundary between byte-level recovery and native application behavior.

MIT license.
