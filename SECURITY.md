# Security and recovery boundaries

SessionCrate is early alpha. It handles potentially private conversation and tool-output records. Compressed archives are **not encrypted**. Preserve independent backups before relying on original reclamation.

The runtime CLI makes no network requests. Search results and manifests can still contain sensitive conversation text, project names, paths and identifiers. Keep archives, manifests, quarantines, search results and local validation reports private.

Original reclamation is explicit and checks the archived bytes, current source, activity probes and quarantine contents. `prune` first uses cheap source existence, type, and metadata checks; bundle pruning also checks source age before reading its historical blob. A missing or changed source can be skipped before that blob is read; use `verify` for whole-vault archive integrity. Open-file checks do not prove logical session inactivity and are limited by the invoking user's permissions and process visibility. Do not use or reopen the selected session during cleanup. Unknown activity preserves the selected original. Native application resume is not a verified guarantee.

Do not remove a retained quarantine just to make a retry succeed. Preserve it and the archive, then reproduce the failure with synthetic data. Crashes can leave a private destination-adjacent plaintext restore stage, and interrupted archive work can leave incomplete `.vault-blob-*`, `.vault-tmp-*`, `.bundle-*`, or `.bundle-manifest-*` files inside the vault. Top-level `stats` sums the in-vault temporary files across namespaces; nested `codex` and `bundles` results retain their namespace-specific fields. These files and their logical and allocated bytes are not validated archives. Source quarantines and restore stages outside the vault are not counted. Preserve all of these artifacts for recovery; automatic scavenging and crash cleanup are not provided.

Failed bundle restore cleanup removes only files and directories created by that restore after their filesystem identities are checked. Pre-existing, shared, or raced destination directories are left alone. Gzip and bundle tar decoding stop when the declared source or bundle size is exceeded; the bundle bound includes normal PAX metadata overhead. These checks are early-abort integrity bounds, not a general disk quota or a universal zip-bomb defense.

Treat externally supplied vaults as untrusted input. A forged manifest can claim a large source size, and the standard-library tar reader can allocate extended-header metadata within that declared allowance before member validation. SessionCrate does not impose an independent process-memory quota for that case.

Batch pruning fails closed when a recognized manifest is malformed. Archive work that fails before manifest publication is reported as an error and returns CLI status `2`; an original that is preserved after a published archive is reported as a skip. Use a full intact archive ID for recovery. `--tool codex` can avoid the bundle namespace, but a bundle tool filter still inspects the shared bundle manifest namespace; no tool filter can isolate an unreadable namespace.

## Report a vulnerability privately

Never post credentials, raw histories or an exploit containing private data in a public issue. Use [GitHub private vulnerability reporting](https://github.com/ictechgy/sessioncrate/security/advisories/new) to report security issues without disclosing them publicly.

Private vulnerability reporting is enabled for this repository. There is no published response-time or security-support promise during alpha.
