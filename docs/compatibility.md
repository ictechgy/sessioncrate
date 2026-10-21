# Compatibility and evidence

SessionCrate is early alpha. Byte preservation, adapter parsing and native application resume are separate claims.

| Area | Current evidence | Remaining boundary |
| --- | --- | --- |
| macOS CLI | Local Python 3.12/3.14 unit, subprocess, real-`lsof`, install and synthetic roundtrip checks | Not a guarantee for every filesystem or concurrent actor |
| Linux CLI | The hosted workflow includes Ubuntu jobs | Check the [CI result](https://github.com/ictechgy/sessioncrate/actions/workflows/tests.yml) for the commit in use |
| Python 3.10+ | Declared runtime minimum; CI includes 3.10, 3.12 and 3.14 | Local runs cover 3.12/3.14; other versions depend on hosted CI evidence |
| Codex | Synthetic rollout archive/search/verify/restore; Codex CLI 0.163.0 reads legacy EventMsg user/assistant history before and after restoration in an offline sandbox | No Codex application version is certified for resume or all pagination layouts |
| Claude Code CLI | Synthetic main/subagent/tool-result groups, binary-safe restoration | No Claude application version is certified for sidebar/resume |
| Grok Build | Synthetic full-directory groups and known ACP text shapes | Parsing is best effort; native resume is unverified |
| Gemini CLI | Synthetic chat JSON, including tool-call results | Native resume is unverified; oversized JSON is skipped by search |
| AGY / GLM / OpenCode / generic exports | Explicit fabricated JSON/JSONL/Markdown/text exports | Not native database cleanup or proof of the underlying model |

On 2026-10-17, two generated-session runs on macOS covered 32 sessions, 52 files, 404 CLI subprocess calls and 498 roundtrip assertions across the adapter modes. Fourteen additional independent checks covered open-file protection, conflicts, corrupt blobs and unrelated corrupt manifests. Those fixtures were disposable and did not contain personal history. This is synthetic validation, not native-app compatibility evidence or a real-session compression forecast.

On 2026-10-20, `python3 Scripts/codex_reader_probe.py` additionally checked the installed Codex CLI 0.163.0 app-server's `thread/read` API against a fabricated legacy EventMsg rollout, before and after real archive/prune/restore. The macOS OS sandbox denied networking and access to real profile directories; the probe used a fresh temporary Codex home without copied credentials. Both user and assistant messages were hydrated from restored turns. This opt-in macOS probe needs Codex and `sandbox-exec`; it does not call `thread/resume` or start a model turn. Its record shape follows the [official history reconstruction tests](https://github.com/openai/codex/blob/main/codex-rs/app-server/tests/suite/v2/thread_read.rs).

## Certifying an application version

For each claimed native version, create a new disposable session with that application in an isolated project, record the exact tool version, and verify that the application can read/resume it before and after archiving/restoring the selected records. Check any sidecars, pagination/dependency files and application-managed indexes. Use only test data and preserve a recovery copy.

Record the operating system, filesystem, source layout, supported/refused cases, actual application result and reproducible commands. Do not copy a user's credentials into a test profile or attach real histories. A passing byte-roundtrip is insufficient to mark native resume as certified.

Until that evidence exists, prefer copy-only archiving and test alternate-destination restoration. Removing old records can leave stale application list/index entries even when the archived bytes are intact.
