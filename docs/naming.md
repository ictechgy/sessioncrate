# Naming decision

Decision date: 2026-10-20. Public name: **SessionCrate**. CLI and Python distribution: `sessioncrate` (12 ASCII letters).

The name describes putting coding-session records into a container that can be searched and restored. It is provider-neutral and does not imply encryption, AI memory, or guaranteed native-session resume.

Tagline: **Pack, search, and restore local AI coding sessions.**

## Consultation

Two providers received a brief describing the product and naming criteria, with no repository source, user transcripts or authentication material in the packet.

| Opinion | Recommendation | Main reason |
| --- | --- | --- |
| Claude | SessionCrate after collision screening | The session/container meaning is explicit and pronounceable |
| Grok | TrailJar | Shorter command and a memorable trail/container metaphor |
| Decision | SessionCrate | Clarity for a first public release is worth the longer command |

The providers disagreed; this is not a consensus claim. Naming opinions are not availability checks.

## Dated collision screen

`codex-vault` already names [another public archive/compact/restore/search CLI](https://github.com/nassim-arifette/codex-vault). Other eliminated candidates include [ColdShelf](https://github.com/CAOShurong/coldshelf), [Trailpack](https://github.com/trailsjs/trailpack), [Tracepack](https://github.com/moritzmyrz/tracepack), [Logfold](https://github.com/sani-savaliya/logfold), and [Sesspack](https://tanelpoder.com/2007/06/24/session-level-statspack/).

Exact-name public GitHub/software searches found no matching SessionCrate software project in this screen. The public [PyPI metadata endpoint](https://pypi.org/pypi/sessioncrate/json) returned HTTP 404 on the decision date. These checks do not reserve a package/repository name and should be repeated before publication. Domain names were not purchased or reserved.

## Compatibility

Only the public branding changes. The internal `codex_vault` module, archive identity salts and manifest formats remain unchanged. Existing `codex-vault` checkout launchers and installed aliases remain usable. New wheels register only `sessioncrate`; old aliases are an explicit source-installer option.

The default vault remains `$XDG_DATA_HOME/codex-vault` or `~/.local/share/codex-vault` for this version. No archive relocation or automatic migration is part of the rename.
