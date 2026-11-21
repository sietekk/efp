#!/usr/bin/env bash
# Fails if a file names the data or secrets directories. Those locations are reached only
# through config.toml. The pattern is assembled from pieces so this script does not match itself.
set -euo pipefail

pattern='efp-''data|\.config/''efp'
status=0
for file in "$@"; do
  if grep -nE -- "$pattern" "$file"; then
    echo "error: $file names the data or secrets directory; read it from config.toml instead" >&2
    status=1
  fi
done
exit "$status"
