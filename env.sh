# Run from the project root: source ./env.sh
if [ ! -f .venv/bin/activate ]; then
  printf '%s\n' 'Create the project .venv before sourcing env.sh.' >&2
  return 1
fi
if [ -d .runtime/node-v24.21.0-darwin-arm64/bin ]; then
  export PATH="$PWD/.runtime/node-v24.21.0-darwin-arm64/bin:$PATH"
fi
. .venv/bin/activate
