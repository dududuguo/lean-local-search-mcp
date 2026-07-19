#!/usr/bin/env bash

set -euo pipefail

if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: python3 is required but was not found in PATH." >&2
  echo "Install Python 3, then run ./setup.sh again." >&2
  exit 1
fi

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
venv_dir="${repo_dir}/.venv"
venv_python="${venv_dir}/bin/python"
server_path="${repo_dir}/tools/lean_local_search_mcp.py"
pip_version="26.1.2"

if [[ ! -x "${venv_python}" ]]; then
  echo "Creating virtual environment at ${venv_dir}"
  if ! python3 -m venv "${venv_dir}"; then
    echo "Error: could not create the virtual environment." >&2
    echo "On Debian/Ubuntu, install python3-venv and try again." >&2
    exit 1
  fi
else
  echo "Reusing virtual environment at ${venv_dir}"
fi

echo "Installing Python dependencies..."
"${venv_python}" -m pip install "pip==${pip_version}"
"${venv_python}" -m pip install -r "${repo_dir}/requirements.txt"

echo "Validating Python entry points..."
"${venv_python}" -m py_compile \
  "${repo_dir}/tools/lean_local_search_mcp.py" \
  "${repo_dir}/tools/fuzz_mcp.py" \
  "${repo_dir}/tools/validate_release.py"

echo
echo "Setup complete. Register the server with Codex after replacing the Lean project path:"
printf 'codex mcp add lean-local-search -- "%s" "%s" --repo "/absolute/path/to/lean-project"\n' \
  "${venv_python}" "${server_path}"
echo
echo "Then verify it with: codex mcp get lean-local-search"
