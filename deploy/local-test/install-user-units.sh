#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
UNIT_SOURCE_DIR="${SCRIPT_DIR}/systemd"
UNIT_TARGET_DIR="${DOC_AGENT_TEST_USER_UNIT_DIR:-/home/inspur/.config/systemd/user}"
RUNTIME_ROOT="${DOC_AGENT_TEST_RUNTIME_ROOT:-/home/inspur/myenv/runtime/doc-agent-test}"
UNITS=(
  doc-agent-test-runtime.service
  doc-agent-test-auto-update.service
  doc-agent-test-auto-update.timer
)

log() {
  printf '[doc-agent-test-unit-install] %s\n' "$*"
}

for command in install systemctl; do
  command -v "${command}" >/dev/null \
    || { log "ERROR: 缺少命令 ${command}" >&2; exit 1; }
done

install -d -m 0755 "${UNIT_TARGET_DIR}"
install -d -m 0700 "${RUNTIME_ROOT}/bin"
install -m 0755 "${SCRIPT_DIR}/manage.sh" "${RUNTIME_ROOT}/bin/.manage.sh.$$"
mv -f "${RUNTIME_ROOT}/bin/.manage.sh.$$" "${RUNTIME_ROOT}/bin/manage.sh"
for unit in "${UNITS[@]}"; do
  [[ -f "${UNIT_SOURCE_DIR}/${unit}" ]] \
    || { log "ERROR: 缺少单元模板 ${unit}" >&2; exit 1; }
  install -m 0644 "${UNIT_SOURCE_DIR}/${unit}" "${UNIT_TARGET_DIR}/${unit}"
done

systemctl --user daemon-reload
systemctl --user enable doc-agent-test-runtime.service >/dev/null
systemctl --user enable doc-agent-test-auto-update.timer >/dev/null
log "已安装稳定 runtime 管理器和自动更新 service/timer；未启动或重启任何服务"
