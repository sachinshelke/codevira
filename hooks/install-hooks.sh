#!/bin/sh
#
# Install Codevira git hooks into .git/hooks/
#
# Run once after cloning or adding this framework to your project:
#   sh .agents/hooks/install-hooks.sh

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOKS_SRC="$REPO_ROOT/.agents/hooks"
HOOKS_DEST="$REPO_ROOT/.git/hooks"

install_hook() {
    name="$1"
    src="$HOOKS_SRC/$name"
    dest="$HOOKS_DEST/$name"

    if [ ! -f "$src" ]; then
        echo "  SKIP: $name (source not found)"
        return
    fi

    if [ -f "$dest" ] && [ ! -L "$dest" ]; then
        echo "  BACKUP: existing $name → $name.bak"
        cp "$dest" "$dest.bak"
    fi

    cp "$src" "$dest"
    chmod +x "$dest"
    echo "  INSTALLED: $name"
}

echo "Installing Codevira git hooks..."
install_hook "post-commit"
echo ""
echo "Done. Hooks installed to $HOOKS_DEST"
echo ""
echo "The post-commit hook will auto-reindex changed source files after every commit."
echo "To verify: make a commit and check .agents/codeindex/.last_indexed updates."
