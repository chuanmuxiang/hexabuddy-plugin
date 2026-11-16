#!/usr/bin/env bash
# Install the "Translate → Buddy" macOS Quick Action.
# The committed workflow carries a placeholder path (so no machine-specific path lives
# in the repo); this script substitutes THIS machine's real path at install time.
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$DIR/translate-to-buddy.py"
DEST="$HOME/Library/Services/Translate to Buddy.workflow"
rm -rf "$DEST"
cp -R "$DIR/Translate to Buddy.workflow" "$DEST"
/usr/bin/sed -i '' "s|__TRANSLATE_PY__|$PY|g" "$DEST/Contents/document.wflow"
/System/Library/CoreServices/pbs -flush 2>/dev/null || true
echo "✓ 'Translate → Buddy' installed."
echo "  Use it: select text in a native app → right-click → Services → Translate → Buddy."
echo "  Bind a hotkey in System Settings → Keyboard → Keyboard Shortcuts → Services → Text."
echo "  Requires this Mac connected (/hexabuddy-workspace:connect) and a buddy online."
