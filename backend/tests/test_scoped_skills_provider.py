from pathlib import Path

from backend.core.skills.scoped_skills_provider import ScopedSkillsProvider


def _write_skill(root: Path, category: str, name: str, owner_id: str | None = None) -> None:
    skill_dir = root / category / name
    skill_dir.mkdir(parents=True)
    owner_line = f"owner_id: {owner_id}\n" if owner_id else ""
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {name} description\n"
        f"{owner_line}"
        "---\n"
        f"# {name}\n",
        encoding="utf-8",
    )


def test_custom_skills_are_visible_only_to_the_declared_owner(tmp_path, monkeypatch):
    _write_skill(tmp_path, "public", "public-skill")
    _write_skill(tmp_path, "custom", "owned-skill", owner_id="user-1")
    _write_skill(tmp_path, "custom", "other-skill", owner_id="user-2")
    _write_skill(tmp_path, "custom", "legacy-ownerless-skill")

    monkeypatch.setattr(
        "backend.core.config.extensions_config.ExtensionsConfig.is_skill_enabled",
        lambda self, name, category: True,
    )

    provider = ScopedSkillsProvider(skills_base_path=tmp_path, user_id="user-1")
    names = {skill.name for skill in provider.get_available_skills_for_user()}

    assert names == {"public-skill", "owned-skill"}
    assert provider.read_skill_content("owned-skill").startswith("---")


def test_custom_skill_cannot_be_read_by_another_user(tmp_path, monkeypatch):
    _write_skill(tmp_path, "custom", "owned-skill", owner_id="user-1")
    monkeypatch.setattr(
        "backend.core.config.extensions_config.ExtensionsConfig.is_skill_enabled",
        lambda self, name, category: True,
    )

    provider = ScopedSkillsProvider(skills_base_path=tmp_path, user_id="user-2")

    try:
        provider.read_skill_content("owned-skill")
    except ValueError as exc:
        assert "not found or not enabled" in str(exc)
    else:
        raise AssertionError("custom skill must not be readable by another user")


def test_skill_cache_reloads_after_ttl_without_pubsub_invalidation(tmp_path, monkeypatch):
    from backend.core.skills import scoped_skills_provider as provider_module

    _write_skill(tmp_path, "public", "first-skill")
    monkeypatch.setattr(
        "backend.core.config.extensions_config.ExtensionsConfig.is_skill_enabled",
        lambda self, name, category: True,
    )
    clock = {"now": 0.0}
    monkeypatch.setattr(provider_module, "_SKILL_CACHE_TTL_SECONDS", 1.0)
    monkeypatch.setattr(provider_module.time, "monotonic", lambda: clock["now"])

    provider = ScopedSkillsProvider(skills_base_path=tmp_path)
    assert {skill.name for skill in provider.get_available_skills_for_user()} == {
        "first-skill"
    }

    _write_skill(tmp_path, "public", "second-skill")
    clock["now"] = 2.0
    assert {skill.name for skill in provider.get_available_skills_for_user()} == {
        "first-skill",
        "second-skill",
    }


def test_process_wide_scan_cache_is_shared_across_provider_instances(
    tmp_path, monkeypatch
):
    from backend.core.skills import scoped_skills_provider as provider_module

    _write_skill(tmp_path, "public", "shared-skill")
    monkeypatch.setattr(
        "backend.core.config.extensions_config.ExtensionsConfig.is_skill_enabled",
        lambda self, name, category: True,
    )
    scans = {"n": 0}
    original_load_skills = provider_module.load_skills

    def _tracked_load_skills(*args, **kwargs):
        scans["n"] += 1
        return original_load_skills(*args, **kwargs)

    monkeypatch.setattr(provider_module, "load_skills", _tracked_load_skills)

    first = ScopedSkillsProvider(skills_base_path=tmp_path, user_id="user-1")
    second = ScopedSkillsProvider(skills_base_path=tmp_path, user_id="user-2")
    assert {s.name for s in first.get_available_skills_for_user()} == {"shared-skill"}
    scans_after_first = scans["n"]
    assert {s.name for s in second.get_available_skills_for_user()} == {"shared-skill"}

    # The second, request-scoped provider must reuse the process-wide scan.
    assert scans["n"] == scans_after_first


def test_invalidate_all_skill_caches_forces_a_fresh_scan(tmp_path, monkeypatch):
    from backend.core.skills.scoped_skills_provider import (
        invalidate_all_skill_caches,
    )

    _write_skill(tmp_path, "public", "first-skill")
    monkeypatch.setattr(
        "backend.core.config.extensions_config.ExtensionsConfig.is_skill_enabled",
        lambda self, name, category: True,
    )

    provider = ScopedSkillsProvider(skills_base_path=tmp_path)
    assert {s.name for s in provider.get_available_skills_for_user()} == {
        "first-skill"
    }

    _write_skill(tmp_path, "public", "second-skill")
    invalidate_all_skill_caches()

    fresh_provider = ScopedSkillsProvider(skills_base_path=tmp_path)
    assert {s.name for s in fresh_provider.get_available_skills_for_user()} == {
        "first-skill",
        "second-skill",
    }
    assert {s.name for s in provider.get_available_skills_for_user()} == {
        "first-skill",
        "second-skill",
    }
