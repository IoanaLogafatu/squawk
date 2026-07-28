#!/bin/bash
set -euo pipefail

DEST_DIR="AI-dump"

# Flush out old files by removing and recreating the destination directory
rm -rf "$DEST_DIR"
mkdir -p "$DEST_DIR"

git ls-files --cached --others --exclude-standard | while IFS= read -r file; do
    # Skip AI-dump directory contents if present
    if [[ "$file" == "$DEST_DIR"* ]]; then
        continue
    fi

    if [[ -f "$file" ]]; then
        flattened_name="${file//\//#}"
        cp "$file" "$DEST_DIR/$flattened_name"
    fi
done

echo "Successfully dumped files into $DEST_DIR."
