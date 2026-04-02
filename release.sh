#!/usr/bin/env bash
set -e

if [ -z "$1" ]; then
  echo "Usage: ./release.sh <version>  (e.g. ./release.sh 0.1.1)"
  exit 1
fi

VERSION="$1"

echo "Releasing v$VERSION..."

# Bump version in pyproject.toml
sed -i '' "s/^version = \".*\"/version = \"$VERSION\"/" pyproject.toml

# Bump version in __init__.py
sed -i '' "s/__version__ = \".*\"/__version__ = \"$VERSION\"/" src/blinkvault/__init__.py

# Commit, tag, push
git add pyproject.toml src/blinkvault/__init__.py
git commit -m "Release v$VERSION"
git tag "v$VERSION"
git push && git push origin "v$VERSION"

echo "Done — GitHub Actions will publish v$VERSION to PyPI."
