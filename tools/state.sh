#!/usr/bin/env bash
# The bot's records (queue, reviews, Telegram state, sources, stats, corrections...) live in a PRIVATE
# repo, never in this public one. Each workflow copies them in at the start and pushes them back at
# the end (copies, not symlinks: files are written by atomic replace, which would break a link).
#
#   bash tools/state.sh pull            # after actions/checkout
#   bash tools/state.sh save "message"  # at the end (if: always())
#
# env: STATE_TOKEN (a token that can push to the private repo), STATE_REPO (default <owner>/quran-shorts-data),
#      STATE_URL (tests: a local repo instead)
set -euo pipefail
REPO="${STATE_REPO:-${GITHUB_REPOSITORY%%/*}/quran-shorts-data}"
DIR=_state
DIRS="queue output data/corrections data/sheikhs"
FILES="data/history.json data/reviews.json data/telegram_state.json data/sources.json data/sources_seen.json
       data/stats.json data/tuning.json data/sounds.json data/footage_checks.json"
# rendered media is never kept (as in .gitignore)
EXCL=(--exclude '*.mp4' --exclude '*.wav' --exclude '*.tmp' --exclude 'visual_test/' --exclude '_*')

case "${1:-}" in
  pull)
    : "${STATE_TOKEN:?STATE_TOKEN secret missing}"
    rm -rf "$DIR"
    git clone -q --depth 1 "${STATE_URL:-https://x-access-token:${STATE_TOKEN}@github.com/${REPO}.git}" "$DIR"
    # identity for the commit AND for "pull --rebase" (it rewrites commits when jobs save at once)
    git -C "$DIR" config user.name "quran-shorts bot"
    git -C "$DIR" config user.email "bot@users.noreply.github.com"
    for d in $DIRS; do
      mkdir -p "$d" "$DIR/$d"
      rsync -a "${EXCL[@]}" "$DIR/$d/" "$d/"
    done
    for f in $FILES; do
      if [ -f "$DIR/$f" ]; then mkdir -p "$(dirname "$f")"; cp "$DIR/$f" "$f"; fi
    done
    echo "state: $(find "$DIR" -type f -not -path "$DIR/.git/*" | wc -l) files from the private repo"
    ;;
  save)
    [ -d "$DIR/.git" ] || { echo "state: nothing pulled, nothing to save"; exit 0; }
    for d in $DIRS; do
      [ -d "$d" ] || continue
      mkdir -p "$DIR/$d"
      rsync -a --delete "${EXCL[@]}" "$d/" "$DIR/$d/"     # --delete: what this job removed (cleanup)
    done
    for f in $FILES; do
      if [ -f "$f" ]; then mkdir -p "$DIR/$(dirname "$f")"; cp "$f" "$DIR/$f"; fi
    done
    cd "$DIR"
    git add -A
    if git diff --cached --quiet; then echo "state: no changes"; exit 0; fi
    git -c user.name="quran-shorts bot" -c user.email="bot@users.noreply.github.com" \
        commit -q -m "${2:-state}"
    for i in 1 2 3 4 5; do
      # same file changed by another job meanwhile: this job's version wins (-X theirs = ours, in a rebase)
      if git pull -q --rebase -X theirs && git push -q; then echo "state: saved"; exit 0; fi
      git rebase --abort 2>/dev/null || true
      sleep $((i * 5))
    done
    # never lose a job's work: park it on its own branch (merge it later), and fail loudly
    git push -q origin "HEAD:refs/heads/rescue-${GITHUB_RUN_ID:-$(date +%s)}" && \
      echo "state: could not merge — saved on branch rescue-${GITHUB_RUN_ID:-?}" >&2
    echo "state: could not push after 5 tries" >&2
    exit 1
    ;;
  *)
    echo "usage: $0 pull | save MESSAGE" >&2
    exit 2
    ;;
esac
