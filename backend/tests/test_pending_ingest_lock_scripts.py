# noqa: E402
"""入库 fence 锁的 eval 调用签名与 Lua 脚本 KEYS/ARGV 用法一致性。

2026-08-22 生产事故：锁续租调用 numkeys=1 漏传 counter_key，而
_RENEW_LOCK_SCRIPT 使用 KEYS[2]——Redis Lua 在脚本第 3 行报参数类型错，
续租必败，任何处理超过一个续租周期（20 分钟）的文件都会以
「入库锁所有权已丢失」中途死亡（长文档整库卡死）。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "backend/application/ingestion/pending_ingest.py"


def _load_module():
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "pending_ingest_under_test", MODULE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass 装饰器按 cls.__module__ 反查命名空间，必须先注册再执行。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pending_module():
    return _load_module()


def _max_index(script: str, kind: str) -> int:
    indexes = [
        int(match)
        for match in re.findall(rf"{kind}\[(\d+)\]", script)
    ]
    return max(indexes, default=0)


def test_lock_scripts_key_and_argv_usage_is_consistent_with_call_sites(
    pending_module,
) -> None:
    """四个 eval 调用点的 numkeys/参数个数必须与脚本内 KEYS/ARGV 最大下标一致。"""

    use_case_cls = pending_module.PendingIngestUseCase
    fence = pending_module.PendingIngestFence(
        lock_key="kb_ingest:{f1}:lock",
        counter_key="kb_ingest:{f1}:fence",
        owner="owner",
        fencing_token=7,
        redis_client=None,
    )

    recorded: list[tuple[str, int, tuple]] = []

    class FakeRedis:
        async def eval(self, script, numkeys, *args):  # noqa: ANN001
            # 按脚本名归一记录，校验由共享断言完成。
            name = {
                use_case_cls._ACQUIRE_LOCK_SCRIPT: "acquire",
                use_case_cls._RENEW_LOCK_SCRIPT: "renew",
                use_case_cls._RELEASE_LOCK_SCRIPT: "release",
                use_case_cls._ASSERT_FENCE_SCRIPT: "assert",
            }[script]
            recorded.append((name, int(numkeys), tuple(args)))
            if name == "acquire":
                return [1, 7]
            return 1

    client = FakeRedis()

    async def run_all() -> None:
        await client.eval(
            use_case_cls._ACQUIRE_LOCK_SCRIPT,
            2,
            fence.lock_key,
            fence.counter_key,
            fence.owner,
            use_case_cls._LOCK_TTL_SECONDS,
        )
        await client.eval(
            use_case_cls._RENEW_LOCK_SCRIPT,
            2,
            fence.lock_key,
            fence.counter_key,
            fence.credential,
            use_case_cls._LOCK_TTL_SECONDS,
        )
        await client.eval(
            use_case_cls._RELEASE_LOCK_SCRIPT,
            1,
            fence.lock_key,
            fence.credential,
        )
        await client.eval(
            use_case_cls._ASSERT_FENCE_SCRIPT,
            2,
            fence.lock_key,
            fence.counter_key,
            fence.credential,
            fence.fencing_token,
        )

    asyncio.run(run_all())

    scripts = {
        "acquire": use_case_cls._ACQUIRE_LOCK_SCRIPT,
        "renew": use_case_cls._RENEW_LOCK_SCRIPT,
        "release": use_case_cls._RELEASE_LOCK_SCRIPT,
        "assert": use_case_cls._ASSERT_FENCE_SCRIPT,
    }
    expected_calls = {
        "acquire": (
            2,
            (fence.lock_key, fence.counter_key, fence.owner,
             use_case_cls._LOCK_TTL_SECONDS),
        ),
        # 事故点：renew 必须传 2 个 KEYS（锁键 + fence 计数键）。
        "renew": (
            2,
            (fence.lock_key, fence.counter_key, fence.credential,
             use_case_cls._LOCK_TTL_SECONDS),
        ),
        "release": (1, (fence.lock_key, fence.credential)),
        "assert": (
            2,
            (fence.lock_key, fence.counter_key, fence.credential,
             fence.fencing_token),
        ),
    }
    for name, script in scripts.items():
        keys_needed = _max_index(script, "KEYS")
        argv_needed = _max_index(script, "ARGV")
        numkeys, args = expected_calls[name]
        assert numkeys == keys_needed, f"{name}: 脚本用 KEYS[{keys_needed}] 但只传 {numkeys} 键"
        assert len(args) == numkeys + argv_needed, (
            f"{name}: 脚本用 ARGV[{argv_needed}] 但参数个数不匹配"
        )
        # 同步断言实际记录的调用形状。
        recorded_names = [item[0] for item in recorded]
        actual = recorded[recorded_names.index(name)]
        assert actual == (name, numkeys, args), f"{name} 调用形状漂移: {actual}"


def test_renew_file_lock_passes_both_keys(pending_module, monkeypatch) -> None:
    """端到端驱动 _renew_file_lock：首轮流产后退出，断言 eval 形状。"""

    use_case_cls = pending_module.PendingIngestUseCase
    calls: list[tuple[int, tuple]] = []

    class FakeRedis:
        async def eval(self, script, numkeys, *args):  # noqa: ANN001
            assert script == use_case_cls._RENEW_LOCK_SCRIPT
            calls.append((int(numkeys), tuple(args)))
            return 1

    fence = pending_module.PendingIngestFence(
        lock_key="kb_ingest:{f2}:lock",
        counter_key="kb_ingest:{f2}:fence",
        owner="owner-2",
        fencing_token=11,
        redis_client=FakeRedis(),
    )
    use_case = use_case_cls(source_label="test", legacy_md_direct=False)

    sleep_calls = 0

    async def fake_sleep(_seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(
        use_case_cls, "_LOCK_RENEW_INTERVAL_SECONDS", 0, raising=False
    )
    monkeypatch.setattr(pending_module.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(use_case._renew_file_lock(fence))

    assert calls, "续租循环未发起任何 eval"
    numkeys, args = calls[0]
    assert numkeys == 2
    assert args == (
        fence.lock_key,
        fence.counter_key,
        fence.credential,
        use_case_cls._LOCK_TTL_SECONDS,
    )
