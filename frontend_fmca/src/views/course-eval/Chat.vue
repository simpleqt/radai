<script setup lang="ts">
/**
 * 课程评价智能对话（地下水式薄壳页）。
 * 直接拖拽/选择上传材料 + 描述任务，course_eval 智能体自动建任务、
 * 绑定材料、跑完整条评价流水线，分数核对与目标映射全在对话里确认。
 */
import AgentChatView from '@/views/ne-full/index.vue';
</script>

<template>
  <AgentChatView
    agent-code="course_eval"
    base-path="/eval/chat"
    brand-name="课程评价智能体"
    brand-icon="GraduationCap"
  />
</template>
