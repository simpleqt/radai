#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
RUNTIME_ROOT="${DOC_AGENT_TEST_RUNTIME_ROOT:-/home/inspur/myenv/runtime/doc-agent-docker-test}"
HOST_TEST_ROOT="${DOC_AGENT_HOST_TEST_ROOT:-/home/inspur/myenv/runtime/doc-agent-test}"
SOURCE_GIT_DIR="${DOC_AGENT_TEST_SOURCE_GIT_DIR:-}"
CURRENT_LINK="${RUNTIME_ROOT}/current"
STATE_DIR="${RUNTIME_ROOT}/state"
REPORT_DIR="${RUNTIME_ROOT}/reports"
DEPLOYMENT_ENV="${STATE_DIR}/deployment.env"
QUALITY_POLICY="${SCRIPT_DIR}/../quality-gate.json"
TEST_BRANCH="${DOC_AGENT_TEST_BRANCH:-dev}"
DEPLOYMENT_BRANCH="${DOC_AGENT_PRODUCTION_BRANCH:-production}"
CREDENTIAL_FILE="${DOC_AGENT_TEST_GIT_CREDENTIAL_FILE:-}"
SUCCESS_SHA_FILE="${STATE_DIR}/auto-update.success-sha"
PROMOTED_SHA_FILE="${STATE_DIR}/production-promotion.success-sha"
TARGET_SHA="${1:-}"
RUNTIME_UNIT="${DOC_AGENT_TEST_RUNTIME_UNIT-doc-agent-test-runtime.service}"
RUNTIME_TIMER="${DOC_AGENT_TEST_RUNTIME_TIMER-doc-agent-docker-test-auto-update.timer}"

if [[ -z "${SOURCE_GIT_DIR}" ]]; then
  if [[ -d "${RUNTIME_ROOT}/source.git" ]]; then
    SOURCE_GIT_DIR="${RUNTIME_ROOT}/source.git"
  else
    SOURCE_GIT_DIR="${HOST_TEST_ROOT}/source.git"
  fi
fi
if [[ -z "${CREDENTIAL_FILE}" ]]; then
  if [[ -f "${RUNTIME_ROOT}/config/git-auth/credentials" ]]; then
    CREDENTIAL_FILE="${RUNTIME_ROOT}/config/git-auth/credentials"
  else
    CREDENTIAL_FILE="${HOST_TEST_ROOT}/config/git-auth/credentials"
  fi
fi

log() {
  printf '[doc-agent-production-promotion] %s\n' "$*"
}

fail() {
  printf '[doc-agent-production-promotion] ERROR: %s\n' "$*" >&2
  exit 1
}

read_sha_file() {
  local path="$1" value=""
  [[ -f "${path}" ]] || fail "缺少测试成功状态文件：${path}"
  IFS= read -r value <"${path}" || true
  [[ "${value}" =~ ^[0-9a-f]{40}$ ]] || fail "测试成功状态不是完整 SHA"
  printf '%s' "${value}"
}

record_sha() {
  local path="$1" sha="$2" temporary="${1}.$$"
  printf '%s\n' "${sha}" >"${temporary}"
  chmod 600 "${temporary}"
  mv -f "${temporary}" "${path}"
}

[[ "${TEST_BRANCH}" == "dev" ]] || fail "测试分支必须为 dev"
[[ "${DEPLOYMENT_BRANCH}" == "production" ]] \
  || fail "正式部署分支必须为 production"
[[ -d "${SOURCE_GIT_DIR}" ]] || fail "测试 bare Git 仓库不存在"
[[ -f "${CREDENTIAL_FILE}" ]] || fail "Git 凭据文件不存在"
[[ -f "${QUALITY_POLICY}" ]] || fail "质量门禁策略不存在"
credential_mode="$(stat -c '%a' "${CREDENTIAL_FILE}")"
credential_mode_value=$((8#${credential_mode}))
(( (credential_mode_value & 077) == 0 )) \
  || fail "Git 凭据文件不能允许 group/other 读取"

runtime_kind=legacy
current_release=""
manager=""
if [[ -f "${DEPLOYMENT_ENV}" ]]; then
  runtime_kind=docker
  deployment_mode="$(awk -F= '$1 == "DEPLOYMENT_MODE" {print $2}' "${DEPLOYMENT_ENV}")"
  [[ "${deployment_mode}" == primary ]] \
    || fail "只有 Docker 主测试环境可以提升到 production"
  current_sha="$(awk -F= '$1 == "ACTIVE_SOURCE_SHA" {print $2}' "${DEPLOYMENT_ENV}")"
  manager="${DOC_AGENT_TEST_MANAGER:-${RUNTIME_ROOT}/control/deploy/docker-test/manage.sh}"
  [[ -x "${manager}" ]] || fail "Docker 测试控制器不可执行"
else
  [[ -L "${CURRENT_LINK}" ]] || fail "测试 current release 不存在"
  current_release="$(readlink -f "${CURRENT_LINK}")"
  case "${current_release}" in
    "${RUNTIME_ROOT}/releases"/*) ;;
    *) fail "测试 current 未指向隔离 releases 目录" ;;
  esac
  current_sha="$(git -C "${current_release}" rev-parse HEAD)"
fi
if [[ -z "${TARGET_SHA}" ]]; then
  TARGET_SHA="${current_sha}"
fi
[[ "${TARGET_SHA}" =~ ^[0-9a-f]{40}$ ]] || fail "目标必须是完整的 40 位 SHA"
[[ "${current_sha}" == "${TARGET_SHA}" ]] \
  || fail "目标 SHA 不是当前测试 release"
[[ "$(read_sha_file "${SUCCESS_SHA_FILE}")" == "${TARGET_SHA}" ]] \
  || fail "目标 SHA 尚未记录为测试成功"
if [[ "${runtime_kind}" == docker && -n "${RUNTIME_TIMER}" ]]; then
  command -v systemctl >/dev/null || fail "缺少 systemctl，无法确认自动更新定时器"
  systemctl --user is-active --quiet "${RUNTIME_TIMER}" \
    || fail "${RUNTIME_TIMER} 未运行，拒绝提升到 production"
elif [[ "${runtime_kind}" == legacy && -n "${RUNTIME_UNIT}" ]]; then
  command -v systemctl >/dev/null || fail "缺少 systemctl，无法确认测试运行状态"
  systemctl --user is-active --quiet "${RUNTIME_UNIT}" \
    || fail "${RUNTIME_UNIT} 未运行，拒绝提升到 production"
fi
if [[ "${runtime_kind}" == docker ]]; then
  "${manager}" status >/dev/null
  "${manager}" smoke
else
  "${current_release}/deploy/local-test/manage.sh" assert-running
fi

export GIT_TERMINAL_PROMPT=0
export GIT_CONFIG_COUNT=2
export GIT_CONFIG_KEY_0=credential.helper
export GIT_CONFIG_VALUE_0=
export GIT_CONFIG_KEY_1=credential.helper
export GIT_CONFIG_VALUE_1="store --file=${CREDENTIAL_FILE}"

git --git-dir="${SOURCE_GIT_DIR}" fetch \
  --prune \
  origin \
  "+refs/heads/${TEST_BRANCH}:refs/heads/${TEST_BRANCH}" \
  "+refs/heads/${DEPLOYMENT_BRANCH}:refs/heads/${DEPLOYMENT_BRANCH}"

test_tip="$(git --git-dir="${SOURCE_GIT_DIR}" rev-parse "refs/heads/${TEST_BRANCH}")"
deployment_tip="$(git --git-dir="${SOURCE_GIT_DIR}" rev-parse "refs/heads/${DEPLOYMENT_BRANCH}")"
[[ "${test_tip}" == "${TARGET_SHA}" ]] \
  || fail "origin/${TEST_BRANCH} 已移动，拒绝提升旧测试提交"

python3 - \
  "${REPORT_DIR}" "${TARGET_SHA}" "${QUALITY_POLICY}" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

report_dir = Path(sys.argv[1])
target_sha = sys.argv[2]
policy_path = Path(sys.argv[3])


def latest_report(pattern: str, label: str):
    reports = []
    for path in report_dir.glob(pattern):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            completed_at = int(payload.get("completed_at_unix") or 0)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        reports.append((completed_at, path, payload))
    if not reports:
        raise SystemExit(f"没有可验证的{label}报告")
    _completed_at, latest_path, latest = max(reports, key=lambda item: item[0])
    if latest.get("status") != "passed" or latest.get("git_sha") != target_sha:
        raise SystemExit(f"最新{label}报告不属于已通过目标 SHA: {latest_path}")
    return latest_path, latest


latest_report("smoke-*.json", "测试业务冒烟")

browser_path, browser = latest_report("browser-*.json", "真实浏览器冒烟")
expected_frontends = {
    "frontend",
    "audit-frontend",
    "csh-frontend",
    "fmca-frontend",
    "guest-frontend",
}
browser_results = browser.get("results")
if not isinstance(browser_results, list):
    raise SystemExit(f"真实浏览器冒烟报告缺少结果列表: {browser_path}")
browser_names = {
    item.get("name")
    for item in browser_results
    if isinstance(item, dict) and item.get("status") == "passed"
}
# 包含式校验：五套生产前端必须全部通过；测试环境新增前端（如
# landslide-frontend，生产接线延后）以超集出现时不得阻断晋升，
# 但报告中任何失败结果都必须阻断。
failed_names = {
    item.get("name")
    for item in browser_results
    if isinstance(item, dict) and item.get("status") != "passed"
}
if not expected_frontends <= browser_names:
    raise SystemExit(f"真实浏览器冒烟未覆盖全部五套生产前端: {browser_path}")
if failed_names:
    raise SystemExit(f"真实浏览器冒烟存在失败前端 {sorted(failed_names)}: {browser_path}")

quality_path, quality = latest_report("quality-*.json", "离线全量质量门禁")
policy_raw = policy_path.read_bytes()
policy = json.loads(policy_raw)
policy_sha256 = hashlib.sha256(policy_raw).hexdigest()
if quality.get("format_version") != policy.get("format_version"):
    raise SystemExit(f"质量报告格式与当前门禁策略不一致: {quality_path}")
if quality.get("policy_id") != policy.get("policy_id"):
    raise SystemExit(f"质量报告策略标识与当前门禁策略不一致: {quality_path}")
if quality.get("policy_sha256") != policy_sha256:
    raise SystemExit(f"质量报告不是由当前门禁策略生成: {quality_path}")
checks = quality.get("checks")
if not isinstance(checks, dict):
    raise SystemExit(f"质量报告缺少检查明细: {quality_path}")
required_checks = set(policy.get("required_checks") or [])
if not required_checks or any(
    not isinstance(checks.get(name), dict)
    or checks[name].get("status") != "passed"
    for name in required_checks
):
    raise SystemExit(f"质量报告存在缺失或未通过的必要检查: {quality_path}")
tests = quality.get("tests") or {}
coverage = quality.get("coverage") or {}
if int(tests.get("passed") or 0) < int(policy["minimum_passed_tests"]):
    raise SystemExit(f"质量报告通过用例数低于当前门禁: {quality_path}")
if int(tests.get("skipped") or 0) > int(policy["maximum_skipped_tests"]):
    raise SystemExit(f"质量报告跳过用例数高于当前门禁: {quality_path}")
if float(coverage.get("percent") or 0) < float(
    policy["minimum_coverage_percent"]
):
    raise SystemExit(f"质量报告覆盖率低于当前门禁: {quality_path}")
PY

if [[ "${deployment_tip}" == "${TARGET_SHA}" ]]; then
  record_sha "${PROMOTED_SHA_FILE}" "${TARGET_SHA}"
  log "origin/${DEPLOYMENT_BRANCH} 已经是测试通过提交 ${TARGET_SHA}"
  exit 0
fi

git --git-dir="${SOURCE_GIT_DIR}" merge-base --is-ancestor \
  "${deployment_tip}" "${TARGET_SHA}" \
  || fail "production 不能快进到测试提交，拒绝覆盖或强推"

git --git-dir="${SOURCE_GIT_DIR}" push \
  origin \
  "${TARGET_SHA}:refs/heads/${DEPLOYMENT_BRANCH}"
git --git-dir="${SOURCE_GIT_DIR}" fetch \
  origin \
  "+refs/heads/${DEPLOYMENT_BRANCH}:refs/heads/${DEPLOYMENT_BRANCH}"
[[ "$(git --git-dir="${SOURCE_GIT_DIR}" rev-parse "refs/heads/${DEPLOYMENT_BRANCH}")" == "${TARGET_SHA}" ]] \
  || fail "推送后 origin/${DEPLOYMENT_BRANCH} 未落在目标 SHA"

record_sha "${PROMOTED_SHA_FILE}" "${TARGET_SHA}"
log "已将同一测试通过提交快进到 origin/${DEPLOYMENT_BRANCH}：${TARGET_SHA}"
log "正式服务未重启；后续只允许从 ${DEPLOYMENT_BRANCH} 准备不可变 release"
