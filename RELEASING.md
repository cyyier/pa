# Releasing Pa

One version number, kept in sync by `tests/test_release.py`:
`pa_lang/__init__.py` (`__version__`, the wheel reads it) = `vscode-pa/package.json` (`version`) = top entry of `CHANGELOG.md`.

## Checklist
1. Update `CHANGELOG.md` (new `## [x.y.z] - date` section) and bump both version strings.
2. `pytest -q` and `(cd vscode-pa && npm test)` must be green.
3. Build and verify the artifacts:
   ```bash
   pip install -e ".[release]"
   rm -rf dist && python -m build            # dist/pa_lang-x.y.z.tar.gz + .whl
   twine check dist/*
   (cd vscode-pa && npm ci && npx @vscode/vsce package -o ../dist/pa-lang-x.y.z.vsix)
   ```
4. Dry run on TestPyPI, then a clean-venv smoke test:
   ```bash
   twine upload --repository testpypi dist/*
   python -m venv /tmp/v && /tmp/v/bin/pip install --index-url https://test.pypi.org/simple/ \
        --extra-index-url https://pypi.org/simple/ pa-compiler && /tmp/v/bin/pac doctor
   ```
5. Commit, tag and push: `git tag -a vX.Y.Z -m "Pa X.Y.Z" && git push origin main vX.Y.Z`.
   The `release` workflow then builds everything, attaches the wheel/sdist/`.vsix` to a GitHub release and
   publishes to PyPI through trusted publishing (configure the project once on pypi.org: owner `cyyier`, repo `pa`,
   workflow `release.yml`, environment `pypi`).
6. VS Code Marketplace / Open VSX (manual, needs a publisher account named `cyyier` and a PAT):
   `npx @vscode/vsce publish` / `npx ovsx publish`.

## Support matrix
Linux x86-64 (tested), CPython 3.11-3.13 with a shared `libpython` and headers, `gcc`. Run `pac doctor` to verify a machine.
