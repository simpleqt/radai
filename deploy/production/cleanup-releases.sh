#!/usr/bin/env bash
# releases/ 保留期清理：每个 SHA 的 worktree + .venv + 5×node_modules + dist
# 是 GB 级目录，只增不删终将写满盘使部署链停摆。
#
# 保留：current 指向的 release、state 里登记的回滚目标、最近 N 个
# （默认 3）。其余默认 dry-run 列表，--apply 才真正删除。
set -Eeuo pipefail
umask 077

RUNTIME_ROOT="${DOC_AGENT_PRODUCTION_RUNTIME_ROOT:-/home/inspur/myenv/runtime/doc-agent-production}"
RELEASES_DIR="${RUNTIME_ROOT}/releases"
STATE_DIR="${RUNTIME_ROOT}/state"
SOURCE_GIT="${RUNTIME_ROOT}/source.git"
KEEP_RECENT="${KEEP_RECENT:-3}"

log() {
  printf '[cleanup-releases] %s\n' "$*"
}
fail() {
  printf '[cleanup-releases] ERROR: %s\n' "$*" >&2
  exit 1
}

APPLY=false
for arg in "$@"; do
  [[ "${arg}" == "--apply" ]] && APPLY=true
done

[[ -d "${RELEASES_DIR}" ]] || fail "releases 目录不存在"

current_release="$(readlink -f "${RELEASES_DIR}/../current" 2>/dev/null || true)"

# 受保护集合：current + state 中登记过的 SHA（回滚目标线索）
declare -A protected=()
[[ -z "${current_release}" ]] || protected["${current_release}"]=1
for marker in "${STATE_DIR}"/production-promotion.success-sha \
  "${STATE_DIR}"/production-deploy.previous.* \
  "${STATE_DIR}"/production-deploy.rollback.*; do
  [[ -f "${marker}" ]] || continue
  sha="$(cat "${marker}" 2>/dev/null | tr -d '[:space:]')"
  [[ "${#sha}" == 40 ]] || continue
  candidate="${RELEASES_DIR}/${sha}"
  [[ -d "${candidate}" ]] && protected["$(readlink -f "${candidate}")"]=1
done

# 最近一次部署前后的单元快照（unit<TAB>pid<TAB>cwd）里解析出的在役
# release 也要保护：增量部署会刻意让未列入重启计划的单元继续运行旧
# SHA 数周，仅保护 current/promotion 标记会把仍在服役的 release 删掉。
for snapshot in "${STATE_DIR}"/production-units-before.tsv \
  "${STATE_DIR}"/production-units-after.tsv; do
  [[ -f "${snapshot}" ]] || continue
  while IFS=$'\t' read -r _unit _pid cwd; do
    [[ "${cwd}" == "${RELEASES_DIR}"/* ]] || continue
    protected["${cwd}"]=1
  done <"${snapshot}"
done

# 按目录内 HEAD 的提交时间排序（目录名可伪造，内容不可）
mapfile -t all_releases < <(
  for dir in "${RELEASES_DIR}"/*/; do
    dir="${dir%/}"
    [[ -d "${dir}" ]] || continue
    commit_ts="$(git -C "${dir}" log -1 --format=%ct 2>/dev/null || echo 0)"
    printf '%s %s\n' "${commit_ts}" "${dir}"
  done | sort -rn
)

candidates=()
for entry in "${all_releases[@]}"; do
  dir="${entry#* }"
  resolved="$(readlink -f "${dir}")"
  [[ -z "${protected[${resolved}]:-}" ]] || continue
  candidates+=("${dir}")
done

# 保留最近 KEEP_RECENT 个候选，其余进入删除清单
if (( ${#candidates[@]} > KEEP_RECENT )); then
  to_remove=("${candidates[@]:KEEP_RECENT}")
else
  to_remove=()
fi

log "共 ${#all_releases[@]} 个 release；受保护 ${#protected[@]} 个；候选保留 ${KEEP_RECENT} 个；待删除 ${#to_remove[@]} 个"

for dir in "${to_remove[@]}"; do
  if [[ "${APPLY}" == "true" ]]; then
    log "删除 ${dir}"
    git --git-dir="${SOURCE_GIT}" worktree remove --force "${dir}" 2>/dev/null \
      || rm -rf -- "${dir:?}"
  else
    log "dry-run: ${dir}"
  fi
done

if [[ "${APPLY}" == "true" ]]; then
  git --git-dir="${SOURCE_GIT}" worktree prune 2>/dev/null || true
  log "清理完成"
else
  log "dry-run 完成；加 --apply 执行删除"
fi
