<script setup lang="ts">
import { computed } from 'vue';
import type { TableViewData } from '../types';

const props = defineProps<{ data: Record<string, unknown> }>();

const table = computed<TableViewData>(
  () => (props.data as unknown as TableViewData) || { columns: [], rows: [] },
);

const alignClass = (align?: string) => {
  if (align === 'right') return 'text-right';
  if (align === 'center') return 'text-center';
  return 'text-left';
};

const cellText = (value: string | number | boolean | null) => {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'boolean') return value ? '是' : '否';
  return String(value);
};
</script>

<template>
  <div v-if="table.columns.length && table.rows.length" class="space-y-1">
    <div class="overflow-x-auto rounded-md border border-border-subtle">
      <table class="w-full min-w-max border-collapse text-xs">
        <thead>
          <tr class="bg-surface-sunken">
            <th
              v-for="column in table.columns"
              :key="column.key"
              class="whitespace-nowrap px-2.5 py-1.5 font-medium text-text-secondary"
              :class="alignClass(column.align)"
            >
              {{ column.label }}
            </th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="(row, rowIndex) in table.rows"
            :key="rowIndex"
            class="border-t border-border-subtle"
          >
            <td
              v-for="column in table.columns"
              :key="column.key"
              class="whitespace-nowrap px-2.5 py-1.5 text-text-primary"
              :class="alignClass(column.align)"
            >
              {{ cellText(row[column.key]) }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <p v-if="table.truncated" class="text-[11px] text-text-tertiary">
      共 {{ table.total_rows }} 行，此处显示前 {{ table.rows.length }} 行
    </p>
  </div>
</template>
