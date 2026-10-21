# Release preparation

SessionCrate is published as an early-alpha source repository at [ictechgy/sessioncrate](https://github.com/ictechgy/sessioncrate). The package is not published to PyPI, and no tagged release is available yet. See the [CI workflow](https://github.com/ictechgy/sessioncrate/actions/workflows/tests.yml) for hosted checks on each commit.

1. **Naming:** The public name is SessionCrate. Preserve legacy formats, data paths and existing checkout aliases. See [naming](naming.md).
2. **Install and documentation:** English and Korean guides, local CLI/skill installation and package smoke checks are included. Build the wheel and source archive, inspect their contents, then run the wheel in an isolated environment outside the checkout.
3. **CI:** The workflow runs the macOS/Ubuntu Python matrix. Check its result for the commit being released; configuration validation and local checks alone do not establish a passing hosted Linux run.
4. **Public boundary:** Keep `reviews/` local. Review commit and package file lists, keep GitHub private vulnerability reporting enabled, and provide only fabricated bug fixtures.
5. **Native compatibility:** Complete the versioned application checks in [compatibility](compatibility.md) before advertising resume support. These checks remain pending.
6. **Package releases:** Before any registry upload or tagged release, recheck the package name, review the final artifacts, and explicitly authorize that release. There is no automatic publishing workflow.

Build commands:

```sh
python3 -m pip install build
python3 -m build
python3 Scripts/package_smoke.py dist/sessioncrate-0.1.0-py3-none-any.whl --sdist dist/sessioncrate-0.1.0.tar.gz
```

If `uv` and its build dependencies are already cached, `uv build --offline --no-config --no-python-downloads` is an alternative local build. Offline builds should not fetch tools or modify global authentication/configuration.
