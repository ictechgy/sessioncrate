# Security and recovery boundaries

SessionCrate is early alpha. It handles potentially private conversation and tool-output records. Compressed archives are **not encrypted**. Preserve independent backups before relying on original reclamation.

The runtime CLI makes no network requests. Search results and manifests can still contain sensitive conversation text, project names, paths and identifiers. Keep archives, manifests, quarantines, search results and local validation reports private.

Original reclamation is explicit and checks the archived bytes, current source, activity probes and quarantine contents. Open-file checks do not prove logical session inactivity. Do not use or reopen the selected session during cleanup. Unknown activity preserves the selected original. Native application resume is not a verified guarantee.

Do not remove a retained quarantine just to make a retry succeed. Preserve it and the archive, then reproduce the failure with synthetic data. Crashes can leave a private destination-adjacent plaintext restore stage; automatic crash cleanup is not provided.

## Report a vulnerability privately

Never post credentials, raw histories or an exploit containing private data in a public issue. Use [GitHub private vulnerability reporting](https://github.com/ictechgy/sessioncrate/security/advisories/new) to report security issues without disclosing them publicly.

Private vulnerability reporting is enabled for this repository. There is no published response-time or security-support promise during alpha.
