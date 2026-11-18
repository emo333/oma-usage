# This file is used only by Open tracked terminal (bash --rcfile).
# Load the usual interactive configuration before installing the session hook.
if [[ -f $HOME/.bashrc && -r $HOME/.bashrc ]]; then
    builtin source "$HOME/.bashrc"
fi
builtin source "${BASH_SOURCE[0]%/*}/oma-usage.bash"
