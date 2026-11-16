#!/usr/bin/env bash
# Remove the "Translate → Buddy" macOS Quick Action.
rm -rf "$HOME/Library/Services/Translate to Buddy.workflow"
/System/Library/CoreServices/pbs -flush 2>/dev/null || true
echo "✓ 'Translate → Buddy' Quick Action removed."
