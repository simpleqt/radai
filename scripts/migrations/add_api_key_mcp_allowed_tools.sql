-- Add an optional per-key MCP tool allowlist. NULL retains current unrestricted behavior.

ALTER TABLE `api_keys`
  ADD COLUMN `mcp_allowed_tools` JSON NULL
    COMMENT 'MCP 工具白名单；NULL 表示不限制'
    AFTER `access_scope`;
