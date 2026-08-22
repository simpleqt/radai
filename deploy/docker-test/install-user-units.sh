#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
RUNTIME_ROOT="${DOC_AGENT_DOCKER_TEST_ROOT:-/home/inspur/myenv/runtime/doc-agent-docker-test}"
CONTROL_RELEASES="${RUNTIME_ROOT}/control-releases"
CONTROL_LINK="${RUNTIME_ROOT}/control"
CONTROL_SHA="${DOC_AGENT_DOCKER_CONTROL_SHA:-$(git -C "${REPO_ROOT}" rev-parse HEAD)}"

umask 077
[[ "${CONTROL_SHA}" =~ ^[0-9a-f]{40}$ ]] || {
  printf '%s\n' 'Docker 测试控制面必须来自完整 Git SHA' >&2
  exit 1
}
repository_sha="$(git -C "${REPO_ROOT}" rev-parse --verify HEAD)"
[[ "${CONTROL_SHA}" == "${repository_sha}" ]] || {
  printf 'Docker 测试控制面 SHA 与当前检出不一致：expected=%s actual=%s\n' \
    "${CONTROL_SHA}" "${repository_sha}" >&2
  exit 1
}
git -C "${REPO_ROOT}" diff --quiet -- \
  deploy/docker-test deploy/local-test deploy/shared-data || {
  printf '%s\n' 'Docker 测试控制面存在未提交改动，拒绝安装' >&2
  exit 1
}
git -C "${REPO_ROOT}" diff --cached --quiet -- \
  deploy/docker-test deploy/local-test deploy/shared-data || {
  printf '%s\n' 'Docker 测试控制面存在未提交暂存改动，拒绝安装' >&2
  exit 1
}
# 未跟踪文件会被 cp -a 一并装进“不可变”控制面，同样拒绝
untracked="$(git -C "${REPO_ROOT}" ls-files --others --exclude-standard -- \
  deploy/docker-test deploy/local-test deploy/shared-data)"
[[ -z "${untracked}" ]] || {
  printf '%s\n' "Docker 测试控制面存在未跟踪文件，拒绝安装：${untracked}" >&2
  exit 1
}

for required_command in cp git install ln mv systemctl systemd-path; do
  command -v "${required_command}" >/dev/null 2>&1 || {
    printf '缺少 %s，无法安装 Docker 测试自动更新单元\n' \
      "${required_command}" >&2
    exit 1
  }
done
USER_CONFIG_ROOT="$(systemd-path user-configuration)"
UNIT_DIR="${USER_CONFIG_ROOT}/systemd/user"

install -d -m 0700 \
  "${RUNTIME_ROOT}/bin" \
  "${CONTROL_RELEASES}" \
  "${UNIT_DIR}"
control_release="${CONTROL_RELEASES}/${CONTROL_SHA}"
if [[ ! -d "${control_release}" ]]; then
  candidate="${CONTROL_RELEASES}/.${CONTROL_SHA}.${BASHPID}"
  [[ ! -e "${candidate}" ]] || exit 1
  install -d -m 0700 "${candidate}/deploy"
  cp -a "${REPO_ROOT}/deploy/docker-test" "${candidate}/deploy/docker-test"
  cp -a "${REPO_ROOT}/deploy/local-test" "${candidate}/deploy/local-test"
  cp -a "${REPO_ROOT}/deploy/shared-data" "${candidate}/deploy/shared-data"
  cp -a "${REPO_ROOT}/deploy/quality-gate.json" \
    "${candidate}/deploy/quality-gate.json"
  printf '%s\n' "${CONTROL_SHA}" >"${candidate}/.source-sha"
  chmod 600 "${candidate}/.source-sha"
  mv -T "${candidate}" "${control_release}"
fi
[[ -f "${control_release}/.source-sha" ]] \
  && [[ "$(<"${control_release}/.source-sha")" == "${CONTROL_SHA}" ]] || {
  printf '%s\n' 'Docker 测试控制面 SHA 标记无效' >&2
  exit 1
}
[[ -x "${control_release}/deploy/docker-test/manage.sh" ]] || {
  printf '%s\n' 'Docker 测试控制面缺少可执行 manager' >&2
  exit 1
}
[[ -f "${control_release}/deploy/quality-gate.json" ]] || {
  printf '%s\n' 'Docker 测试控制面缺少质量门禁策略' >&2
  exit 1
}
temporary_link="${RUNTIME_ROOT}/.control.${BASHPID}"
ln -s "${control_release}" "${temporary_link}"
mv -Tf "${temporary_link}" "${CONTROL_LINK}"

temporary_auto_update="${RUNTIME_ROOT}/bin/.auto-update.sh.${BASHPID}"
install -m 0755 \
  "${control_release}/deploy/docker-test/auto-update.sh" \
  "${temporary_auto_update}"
mv -f "${temporary_auto_update}" "${RUNTIME_ROOT}/bin/auto-update.sh"
install -m 0644 \
  "${control_release}/deploy/docker-test/systemd/doc-agent-docker-test-auto-update.service" \
  "${UNIT_DIR}/doc-agent-docker-test-auto-update.service"
install -m 0644 \
  "${control_release}/deploy/docker-test/systemd/doc-agent-docker-test-auto-update.timer" \
  "${UNIT_DIR}/doc-agent-docker-test-auto-update.timer"
systemctl --user daemon-reload
systemctl --user enable --now doc-agent-docker-test-auto-update.timer
printf 'Docker 主测试控制面 %s 与自动更新 timer 已安装并启用\n' "${CONTROL_SHA}"
