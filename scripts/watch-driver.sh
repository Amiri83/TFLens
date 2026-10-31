#!/bin/bash
# Optional single-cron driver: runs watch.sh for each repo given, or, with no args, for every
# repo armed by launch.sh (/tmp/<repo>-watch.state). watch.sh is silent unless it reports, and
# returns at once (QA runs detached), so one cron can cover all repos.
# Usage: watch-driver.sh [repo ...]
DIR="$(dirname "$(readlink -f "$0")")"
if [ "$#" -eq 0 ]; then
  for f in /tmp/*-watch.state; do
    [ -f "$f" ] || continue
    r="${f##*/}"; r="${r%-watch.state}"
    [ -d "/data/projects/$r" ] && set -- "$@" "$r"
  done
fi
for repo in "$@"; do
  bash "$DIR/watch.sh" "$repo"
done
