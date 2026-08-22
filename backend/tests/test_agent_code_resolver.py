from backend.application.agents.code_resolver import AgentCodeResolver


def test_agent_code_resolver_normalizes_legacy_codes():
    assert AgentCodeResolver.to_runtime("ne_full_feature") == "ne"
    assert AgentCodeResolver.to_runtime("CSH_FULL_FEATURE") == "csh"
    assert AgentCodeResolver.to_runtime("fmca_full_feature") == "fmca"


def test_agent_code_resolver_preserves_runtime_codes():
    assert AgentCodeResolver.to_runtime("ne") == "ne"
    assert AgentCodeResolver.to_runtime("groundwater") == "groundwater"


def test_agent_code_resolver_legacy_alias_and_binding_codes():
    assert AgentCodeResolver.legacy_alias("ne") == "ne_full_feature"
    assert AgentCodeResolver.legacy_alias("fmca") == "fmca_full_feature"
    assert AgentCodeResolver.legacy_alias("groundwater") == "groundwater_full_feature"
    assert AgentCodeResolver.binding_codes("ne_full_feature") == ("ne", "ne_full_feature")
    assert AgentCodeResolver.binding_codes("fmca_full_feature") == ("fmca", "fmca_full_feature")
    assert AgentCodeResolver.binding_codes("fmca") == ("fmca", "fmca_full_feature")
