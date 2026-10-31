#!/usr/bin/env bash
# Training data for our own speech model (src/dataset.py) lives in a PRIVATE repo. Checked out
# sparsely: only index.json comes down, new clips are added and pushed — the audio already there is
# never downloaded by the hourly job.
#
#   bash tools/dataset.sh pull
#   bash tools/dataset.sh save
#
# env: STATE_TOKEN, DATASET_REPO (default <owner>/quran-shorts-dataset)
set -euo pipefail
REPO="${DATASET_REPO:-${GITHUB_REPOSITORY%%/*}/quran-shorts-dataset}"
DIR=_dataset

case "${1:-}" in
  pull)
    : "${STATE_TOKEN:?STATE_TOKEN secret missing}"
    rm -rf "$DIR"
    git clone -q --depth 1 --filter=blob:none --sparse \
        "https://x-access-token:${STATE_TOKEN}@github.com/${REPO}.git" "$DIR"
    # identity for the commit AND for "pull --rebase" (it rewrites commits when jobs save at once)
    git -C "$DIR" config user.name "quran-shorts bot"
    git -C "$DIR" config user.email "bot@users.noreply.github.com"
    echo "dataset: $(python3 -c "import json;print(len(json.load(open('$DIR/index.json'))))" 2>/dev/null || echo 0) clips so far"
    ;;
  save)
    [ -d "$DIR/.git" ] || { echo "dataset: nothing pulled"; exit 0; }
    cd "$DIR"
    git add --sparse -A
    if git diff --cached --quiet; then echo "dataset: nothing new"; exit 0; fi
    git -c user.name="quran-shorts bot" -c user.email="bot@users.noreply.github.com" \
        commit -q -m "clips: $(date -u '+%F %H:%M')"
    for i in 1 2 3 4 5; do
      if git pull -q --rebase && git push -q; then echo "dataset: saved"; exit 0; fi
      git rebase --abort 2>/dev/null || true
      sleep $((i * 5))
    done
    echo "dataset: could not push" >&2
    exit 1
    ;;
  *)
    echo "usage: $0 pull | save" >&2
    exit 2
    ;;
esac
