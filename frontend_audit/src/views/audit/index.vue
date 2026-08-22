<script setup lang="ts">
import AgentChatView from '@/views/ne-full/index.vue';

const auditSuggestions = [
  '高校科研经费审计重点有哪些？',
  '如何识别高校采购中的围标串标风险？',
  '高校建设工程审计应关注哪些环节？',
  '怎样开展高校领导干部经济责任审计？',
];
</script>

<template>
  <AgentChatView
    agent-code="audit"
    base-path="/audit"
    brand-name="高校审计智能助手"
    brand-icon="BadgeCheck"
    empty-logo-src="/university-audit-logo.png"
    empty-logo-alt="高校审计智能体"
    empty-title="你好，我是高校审计智能助手"
    empty-description="我可以帮你查询审计制度、分析风险线索，辅助审计方案与报告编制"
    :empty-suggestions="auditSuggestions"
    empty-suggestion-style="audit"
    empty-logo-compact
    :empty-logo-dark-invert="false"
    qa-only
  />
</template>
