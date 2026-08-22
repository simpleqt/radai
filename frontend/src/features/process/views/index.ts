import type { Component } from 'vue';
import type { DisplayView } from '../types';
import ArtifactView from './ArtifactView.vue';
import IssuesView from './IssuesView.vue';
import KvView from './KvView.vue';
import SourcesView from './SourcesView.vue';
import TableView from './TableView.vue';
import TextView from './TextView.vue';

const VIEW_COMPONENTS: Record<DisplayView, Component> = {
  text: TextView,
  kv: KvView,
  table: TableView,
  issues: IssuesView,
  sources: SourcesView,
  artifact: ArtifactView,
};

export function resolveViewComponent(view: DisplayView): Component {
  return VIEW_COMPONENTS[view] || TextView;
}

export { ArtifactView, IssuesView, KvView, SourcesView, TableView, TextView };
