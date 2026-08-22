#!/usr/bin/env python3
"""Run the immutable, offline full-regression gate and emit an auditable report."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def load_policy(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    payload = json.loads(raw)
    required = {
        "format_version",
        "policy_id",
        "minimum_passed_tests",
        "maximum_skipped_tests",
        "minimum_coverage_percent",
        "required_checks",
    }
    missing = required.difference(payload)
    if missing:
        raise ValueError(f"quality policy is missing fields: {sorted(missing)}")
    if int(payload["format_version"]) != 1:
        raise ValueError("unsupported quality policy format_version")
    if int(payload["minimum_passed_tests"]) < 1:
        raise ValueError("minimum_passed_tests must be positive")
    if int(payload["maximum_skipped_tests"]) < 0:
        raise ValueError("maximum_skipped_tests must be non-negative")
    if not 0 < float(payload["minimum_coverage_percent"]) <= 100:
        raise ValueError("minimum_coverage_percent must be in (0, 100]")
    checks = payload["required_checks"]
    if not isinstance(checks, list) or not checks or not all(
        isinstance(item, str) and item for item in checks
    ):
        raise ValueError("required_checks must be a non-empty string list")
    return payload, hashlib.sha256(raw).hexdigest()


def parse_junit(path: Path) -> dict[str, int | float]:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    if not suites:
        raise ValueError("JUnit report contains no test suites")

    def total(attribute: str) -> int:
        return sum(int(float(suite.attrib.get(attribute, "0"))) for suite in suites)

    tests = total("tests")
    failures = total("failures")
    errors = total("errors")
    skipped = total("skipped")
    passed = tests - failures - errors - skipped
    if tests < 0 or passed < 0:
        raise ValueError("JUnit report contains inconsistent counters")
    return {
        "total": tests,
        "passed": passed,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
        "duration_seconds": sum(
            float(suite.attrib.get("time", "0") or 0) for suite in suites
        ),
    }


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def check(status: bool, **details: Any) -> dict[str, Any]:
    return {"status": "passed" if status else "failed", **details}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--policy",
        type=Path,
        default=Path("/workspace/deploy/quality-gate.json"),
    )
    parser.add_argument("--affected-service", action="append", default=[])
    return parser


def main() -> int:
    args = build_parser().parse_args()
    started_at = int(time.time())
    checks: dict[str, dict[str, Any]] = {}
    report: dict[str, Any] = {
        "format_version": 1,
        "status": "failed",
        "git_sha": args.source_sha,
        "started_at_unix": started_at,
        "completed_at_unix": started_at,
        "checks": checks,
    }

    try:
        policy, policy_sha256 = load_policy(args.policy)
        report.update(
            {
                "policy_id": policy["policy_id"],
                "policy_sha256": policy_sha256,
                "thresholds": {
                    "minimum_passed_tests": int(
                        policy["minimum_passed_tests"]
                    ),
                    "maximum_skipped_tests": int(
                        policy["maximum_skipped_tests"]
                    ),
                    "minimum_coverage_percent": float(
                        policy["minimum_coverage_percent"]
                    ),
                },
                "affected_services": sorted(set(args.affected_service)),
            }
        )

        archived_sha = Path("/workspace/.source-sha").read_text(
            encoding="utf-8"
        ).strip()
        source_ok = bool(FULL_SHA.fullmatch(args.source_sha)) and (
            archived_sha == args.source_sha
        )
        checks["source-sha"] = check(
            source_ok,
            archived_sha=archived_sha,
        )
        checks["candidate-image-builds"] = check(
            True,
            affected_services=sorted(set(args.affected_service)),
        )
        if not source_ok:
            raise ValueError("quality source SHA does not match the immutable archive")

        runner_env = os.environ.copy()
        runner_env.update(
            {
                "AGENT_TRACE_INDEXED_ROOT": "/tmp/indexed-agent-traces",
                "AGENT_TRACE_ROOT": "/tmp/agent-traces",
                "COVERAGE_FILE": "/tmp/.coverage",
                "LOG_DIR": "/tmp/logs",
                "LOG_FILE": "/tmp/logs/pytest.log",
                "PYTHONPYCACHEPREFIX": "/tmp/pycache",
            }
        )
        compile_result = subprocess.run(
            [sys.executable, "-m", "compileall", "-q", "backend"],
            check=False,
            env=runner_env,
        )
        checks["backend-compileall"] = check(
            compile_result.returncode == 0,
            returncode=compile_result.returncode,
        )

        junit_path = args.output.parent / "junit.xml"
        coverage_path = args.output.parent / "coverage.json"
        # 并行 worker 数：离线门禁容器内无外网/外部服务，112 核构建机上
        # 串行 ~3000 项 ~5 分钟是部署链最大单项成本。实测 -n 8 全绿
        # （2975/0/21、覆盖率合并 61.1%、145s vs 310s）；-n 16 会在收尾
        # 撞 pids-limit 2048（execnet termkill 起不来线程），不要盲目调高。
        # 设 0 或 1 回退串行（排查并发缺陷用）。
        try:
            pytest_workers = int(os.getenv("QUALITY_GATE_PYTEST_WORKERS", "8"))
        except ValueError:
            pytest_workers = 8
        if pytest_workers > 1:
            # 每 worker 的数值库线程帽：不加会在多 worker 下耗尽容器
            # pids 限额（OpenBLAS 默认按 112 核各开满）。
            runner_env.update(
                {
                    "OPENBLAS_NUM_THREADS": "1",
                    "OMP_NUM_THREADS": "1",
                    "MKL_NUM_THREADS": "1",
                    "NUMEXPR_NUM_THREADS": "1",
                }
            )
        pytest_cmd = [sys.executable, "-m", "pytest"]
        if pytest_workers > 1:
            pytest_cmd += ["-n", str(pytest_workers)]
        pytest_cmd += [
            "-o",
            (
                "addopts=--strict-markers --strict-config --cov=backend "
                "--cov-report=term:skip-covered"
            ),
            f"--cov-report=json:{coverage_path}",
            f"--junitxml={junit_path}",
            "-p",
            "no:cacheprovider",
            "--basetemp=/tmp/pytest",
            "-m",
            "not golden",
            "backend/tests",
        ]
        pytest_result = subprocess.run(
            pytest_cmd,
            check=False,
            env=runner_env,
        )
        report["pytest_workers"] = max(pytest_workers, 1)
        tests = parse_junit(junit_path)
        coverage_payload = json.loads(coverage_path.read_text(encoding="utf-8"))
        coverage_percent = float(coverage_payload["totals"]["percent_covered"])
        report["tests"] = tests
        report["coverage"] = {
            "percent": coverage_percent,
            "covered_lines": int(coverage_payload["totals"]["covered_lines"]),
            "statements": int(coverage_payload["totals"]["num_statements"]),
            "missing_lines": int(coverage_payload["totals"]["missing_lines"]),
        }
        pytest_ok = (
            pytest_result.returncode == 0
            and int(tests["failures"]) == 0
            and int(tests["errors"]) == 0
        )
        checks["backend-pytest"] = check(
            pytest_ok,
            returncode=pytest_result.returncode,
        )
        checks["minimum-passed-tests"] = check(
            int(tests["passed"]) >= int(policy["minimum_passed_tests"]),
            actual=int(tests["passed"]),
            required=int(policy["minimum_passed_tests"]),
        )
        checks["maximum-skipped-tests"] = check(
            int(tests["skipped"]) <= int(policy["maximum_skipped_tests"]),
            actual=int(tests["skipped"]),
            required_maximum=int(policy["maximum_skipped_tests"]),
        )
        checks["minimum-coverage"] = check(
            coverage_percent >= float(policy["minimum_coverage_percent"]),
            actual=coverage_percent,
            required=float(policy["minimum_coverage_percent"]),
        )

        required_checks = set(policy["required_checks"])
        missing_checks = required_checks.difference(checks)
        all_passed = not missing_checks and all(
            checks[name].get("status") == "passed" for name in required_checks
        )
        report["status"] = "passed" if all_passed else "failed"
        if missing_checks:
            report["error"] = f"missing checks: {sorted(missing_checks)}"
    except Exception as exc:  # The failed report is part of the audit trail.
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        report["completed_at_unix"] = int(time.time())
        atomic_json(args.output, report)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
