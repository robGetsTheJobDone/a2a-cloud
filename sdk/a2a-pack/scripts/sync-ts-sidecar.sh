#!/usr/bin/env bash
# Sync the embedded TypeScript sidecar (a2a_pack/typescript, shipped in the
# Python wheel and vendored into `a2a init --language typescript|javascript`
# projects) from the canonical npm package source in typescript/.
#
#   scripts/sync-ts-sidecar.sh          # copy sources + rebuild dist
#   scripts/sync-ts-sidecar.sh --check  # exit 1 if the embedded copy drifted
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src="${here}/typescript"
dst="${here}/a2a_pack/typescript"
files=(src/index.ts package.json tsconfig.json README.md)

if [[ "${1:-}" == "--check" ]]; then
  status=0
  for f in "${files[@]}"; do
    if ! cmp -s "${src}/${f}" "${dst}/${f}"; then
      echo "drift: ${dst#"${here}/"}/${f} differs from ${src#"${here}/"}/${f}" >&2
      status=1
    fi
  done
  [[ "${status}" -eq 0 ]] || echo "run scripts/sync-ts-sidecar.sh" >&2
  exit "${status}"
fi

for f in "${files[@]}"; do
  mkdir -p "$(dirname "${dst}/${f}")"
  cp "${src}/${f}" "${dst}/${f}"
done

if [[ ! -d "${src}/node_modules" ]]; then
  (cd "${src}" && npm ci --no-audit --no-fund)
fi
rm -rf "${dst}/dist"
(cd "${src}" && npx tsc -p "${dst}/tsconfig.json" --typeRoots "${src}/node_modules/@types")
echo "synced a2a_pack/typescript from typescript/ ($(node -p "require('${src}/package.json').version"))"
