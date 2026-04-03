# blinkvault — Claude Instructions

## "bump it"
When the user says "bump it" (or similar like "bump the version", "ship it", "release it"):
1. Read the current version from `pyproject.toml`
2. Increment the patch number (e.g. 0.1.0 → 0.1.1)
3. Run `./release.sh <new_version>`
4. Report the new version and the GitHub Actions URL to watch the PyPI publish
