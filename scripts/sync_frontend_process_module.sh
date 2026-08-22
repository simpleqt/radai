#!/usr/bin/env bash
#
# 同步四端共享的过程输出模块。
#
# frontend/src/features/process 是唯一源，其余三端是它的副本。
# 用法：
#   scripts/sync_frontend_process_module.sh          # 从源同步到其余三端
#   scripts/sync_frontend_process_module.sh --check  # 只校验一致性（CI 用），不一致时退出码 1
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_DIR="${REPO_ROOT}/frontend/src/features/process"
TARGETS=(frontend_audit frontend_csh_ai frontend_fmca)
CHECK_ONLY=0

if [[ "${1:-}" == "--check" ]]; then
  CHECK_ONLY=1
fi

if [[ ! -d "${SOURCE_DIR}" ]]; then
  echo "error: 源目录不存在: ${SOURCE_DIR}" >&2
  exit 2
fi

drift=0
for target in "${TARGETS[@]}"; do
  target_dir="${REPO_ROOT}/${target}/src/features/process"

  if [[ "${CHECK_ONLY}" -eq 1 ]]; then
    if ! diff -r -q "${SOURCE_DIR}" "${target_dir}" >/dev/null 2>&1; then
      echo "drift: ${target}/src/features/process 与源不一致"
      diff -r -q "${SOURCE_DIR}" "${target_dir}" || true
      drift=1
    fi
    continue
  fi

  mkdir -p "$(dirname "${target_dir}")"
  rm -rf "${target_dir}"
  cp -R "${SOURCE_DIR}" "${target_dir}"
  echo "synced: ${target}/src/features/process"
done

if [[ "${CHECK_ONLY}" -eq 1 ]]; then
  if [[ "${drift}" -ne 0 ]]; then
    echo
    echo "共享过程模块存在漂移。请在 frontend/ 下修改后运行 scripts/sync_frontend_process_module.sh" >&2
    exit 1
  fi
  echo "ok: 四端共享过程模块一致"
fi
