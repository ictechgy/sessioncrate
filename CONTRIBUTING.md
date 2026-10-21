# Contributing to SessionCrate

Focused fixes, source-format adapters and reproducible recovery cases are welcome.

## Work locally

Use Python 3.10 or newer. Runtime code uses only the standard library; original reclamation additionally needs `lsof`.

```sh
bash Scripts/verify.sh
python3 Scripts/roundtrip_check.py --seed 12345
```

The longer roundtrip check requires real `lsof`. Both commands create disposable fixtures. Do not substitute your real agent history, account/configuration directory, credentials, or project worktree.

For a behavior change, add a small regression that fails before the fix and passes afterward. Include relevant corruption, source mutation, restore conflict or activity-check failure cases. Preserve v1 archive identities and the old checkout launchers when changing the public interface.

## Report a bug

Include the SessionCrate version, Python version, operating system, adapter/tool version, command options and a minimal **synthetic** reproducer. Replace private paths and identifiers. Keep the original failing archive/quarantine privately for your recovery; never attach personal transcripts, authentication files, generated search snippets or real archives to a public issue.

Describe the expected result, observed result and exit code. For a source-format change, provide a fabricated record with the same shape, rather than copying a private session. State whether the example covers parsing, byte restoration or the native application's resume behavior.

## Package checks

Build with the PEP 517 backend, then test the built wheel outside the checkout:

```sh
python3 -m pip install build
python3 -m build
python3 Scripts/package_smoke.py dist/sessioncrate-0.1.0-py3-none-any.whl --sdist dist/sessioncrate-0.1.0.tar.gz
```

Inspect both artifacts before release. Local `reviews/`, personal data, credentials, caches, environments and build outputs must not enter source distributions or wheels. The wheel provides the CLI; the source archive also provides the optional Codex skill installer.

Keep pull requests limited to one problem. Explain the trigger, resulting behavior, validation and any remaining limits. Do not publish a release or change the archive format as part of an unrelated fix.
