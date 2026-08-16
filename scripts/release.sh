#!/bin/zsh

set -e

cd "$(git rev-parse --show-toplevel)"

VERSION="$(
  python3 - <<'PY'
import json
from pathlib import Path

manifest = Path("custom_components/tesla_solar_controller/manifest.json")
data = json.loads(manifest.read_text())
print(data["version"])
PY
)"

TAG="v${VERSION}"
SHA="$(git rev-parse HEAD)"

echo "Tesla Solar Controller release"
echo "Version: ${TAG}"
echo

if [ -n "$(git status --porcelain)" ]; then
  echo "ERROR: Working tree is not clean."
  echo "Commit and push all changes before creating a release."
  exit 1
fi

git fetch origin

LOCAL_SHA="$(git rev-parse HEAD)"
REMOTE_SHA="$(git rev-parse origin/main)"

if [ "$LOCAL_SHA" != "$REMOTE_SHA" ]; then
  echo "ERROR: Local main and origin/main are not identical."
  echo "Commit/push or pull before releasing."
  exit 1
fi

if gh release view "$TAG" >/dev/null 2>&1; then
  echo "ERROR: Release ${TAG} already exists."
  exit 1
fi

echo "Checking GitHub validation..."

STATUS="$(
  gh run list \
    --commit "$SHA" \
    --workflow Validate \
    --limit 1 \
    --json status,conclusion \
    --jq '.[0] | "\(.status) \(.conclusion)"'
)"

if [ "$STATUS" != "completed success" ]; then
  echo "ERROR: Latest Validate workflow for this commit is not green."
  echo "Current status: ${STATUS:-no workflow found}"
  exit 1
fi

echo "Validation passed."
echo "Creating ${TAG}..."

gh release create "$TAG" \
  --target main \
  --title "$TAG" \
  --generate-notes \
  --fail-on-no-commits

echo
echo "Release ${TAG} created successfully."
gh release view "$TAG"
