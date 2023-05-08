#! /bin/bash

function error() {
    echo "${1:-Error!}" >&2
    exit 1
}


[ $# == 1 ] || error "Usage: $0 UDB-DIRECTORY"

which curl > /dev/null 2>&1 || error "No curl installed."

readonly udb_dir="$1"
[ -d "$udb_dir" ] || error "Not a directory: $udb_dir"

readonly python_exe="$udb_dir/private/gdb/install/x64/bin/python3"
[ -x "$python_exe" ] || error "Not a UDB directory (no Python executable found): $udb_dir"

readonly target=${XDG_DATA_HOME:-~/.local/share}/undo/altui_packages
mkdir -p "$target" || error "Cannot create target directory: $target"
cd "$target" || error "Cannot cd to directory: $target"

curl https://bootstrap.pypa.io/get-pip.py > get-pip.py
[ -e get-pip.py ] || error "get-pip.py file not found."
"$python_exe" get-pip.py || error "Cannot install pip."

"$python_exe" -m pip install --upgrade --target . \
    pygdbmi \
    pyte \
    textual[dev] \
    || error "Cannot install dependencies."

echo
echo "Successfully installed dependencies!"
