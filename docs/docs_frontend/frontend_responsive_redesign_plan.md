# 前端响应式重设计方案（ChatGPT 式中性极简）

> 状态：**三端主体已落地**（2026-07-25）· 创建于 2026-07-25
> 适用范围：`frontend/`（RadAI）、`frontend_csh_ai/`（CSH-AI）、`frontend_fmca/`（FMCA）；`guest/` 复用 `frontend/src` 组件与 token
> 落地方式：**同一份规范，三处独立落地**（本轮不抽公共包）
> 关联文档：[ne_full_module.md](./ne_full_module.md) · [infrastructure_module.md](./infrastructure_module.md)

---

## 1. 为什么要重做

当前自适应失效的根因不是断点写得不够细，而是**架构上存在两套互相打架的布局系统**。逐条列出证据。

### 1.1 布局由 URL 决定，而不是由容器尺寸决定

`/app`（桌面）与 `/m`（移动）是两条路由指向同一个 `ne-full/index.vue`。选哪条由一次性探测决定：

```ts
// router/index.ts
const detectDefaultMode = (): 'mobile' | 'desktop' => {
  const width = window.innerWidth;
  const uaMobile = /android|webos|iphone|ipad|ipod|.../i.test(ua.toLowerCase());
  return uaMobile || width <= 900 ? 'mobile' : 'desktop';
};
```

`ne-full/index.vue` 里 `watch(effectiveMode)` 会在宽度跨过 900px 时触发 `router.replace`，导致整个视图卸载重挂载——用户拖动窗口看到的是"闪一下重来"，而不是平滑重排。这是体感上"自适应没做好"最直接的来源。

### 1.2 两套断点系统阈值不一致，导致 768–1360px 死区

| 系统 | 使用次数 | 控制什么 |
| :--- | :--- | :--- |
| Tailwind 标准 `sm/md/lg/xl` | 266 处 | 字号、内边距、间距、行内文字显隐 |
| 自定义 `min-[1360px]` | 22 处 | **骨架**：是否显示历史侧栏、是否出现底部 tab bar、输入框是 fixed 还是文档流 |
| 自定义 `min-[1680px]` | 36 处 | 大屏间距放大档 |

两套阈值对不上，于是 **768px–1360px 成为死区**：正文已经用上桌面级字号和间距（`md:` / `lg:` 生效），骨架却仍是移动端的（无侧栏、底部五宫格 tab、输入框悬浮）。这个区间恰好覆盖 iPad 横屏（1180px）、13 寸笔记本缩放后、以及 1080p/1440p 显示器上的半屏窗口（960px / 1280px）——都是高频尺寸。

另外 1359px → 1360px 一个像素的跳变，会同时插入 76px 的 NavRail 和 300px 的 HistorySidebar，内容区瞬间损失 376px。

### 1.3 输入框用 fixed + 写死的避让高度，必然遮挡

```css
/* style.css */
--mobile-chat-height: 10.5rem;  /* 预留输入框高度（含访客提示条 + 工具栏） */
```

`10.5rem` 是常量，但 ChatBox 实际高度会随 textarea 自动增高、访客提示条、附件缩略图而变。只要用户多打两行或传个文件，最后一条消息就被盖住。这是"用常量避让浮动元素"这个思路本身的问题，调数值治不好。

### 1.4 纵向靠 vh 与 max-height 媒体查询硬压

`@media(max-height:900px)` 出现 6 处，空态 logo 按 `max-h-[10vh]` / `7.5vh` / `17vh` 分档跳变。用视口比例防溢出，说明纵向没有正确的收缩机制。

### 1.5 存在定位不住的滚动溢出，被定时器压着

`body.ne-chat-active { overflow: hidden }`，外加路由 `afterEach`、`beforeEach` 与组件 `onMounted` 各自的 `setTimeout(scrollTo, 50/150/300)`——合计 10 次强制归零。这是在用轮询掩盖布局 bug。

### 1.6 装饰元素用混合定位，窄屏必跑偏

外壳里四个绝对定位圆环使用 `left-[45%] top-20 h-80 w-80` 这类"百分比定位 + 固定尺寸"，窄屏下位置失控，目前靠 `[@media(max-height:900px)]:hidden` 掩盖。

---

## 2. 设计原则

1. **一棵 DOM 树。** 所有尺寸共用同一套结构，差异全部由 CSS 与组件状态表达，永不换路由、永不条件渲染两套骨架。
2. **组件按自己拿到的宽度决策，不看视口。** 内容区实际宽度取决于「视口 × 侧栏状态 × 面板状态」，视口断点无法描述，一律改用容器查询。
3. **没有浮动避让。** 所有占位元素在文档流内，靠 flex 分配空间，不存在"预留高度"这类常量。
4. **纵向连续收缩，不分档。** 用 `clamp()` 代替 `vh` 分档；空态自身可滚动，矮屏出现滚动条是正确行为。
5. **视觉靠留白和字重建立层级，不靠渐变和阴影。** 强调色只出现在三个位置：主按钮、导航激活态、焦点环。

---

## 3. 设计 Token

替换 `style.css` 中的 `@theme` 块。所有魔法值收敛到此处，组件内禁止再出现 `#e8eef9`、`shadow-[0_18px_34px_-16px_rgba(...)]` 这类逐处手写值。

```css
@theme {
  /* ── 中性面 ── */
  --color-surface:         #ffffff;
  --color-surface-sunken:  #f7f7f8;   /* 侧栏、次级面板底色 */
  --color-surface-raised:  #ffffff;   /* 卡片、气泡 */
  --color-surface-hover:   #f0f0f1;

  --color-border-subtle:   #e8e8ea;
  --color-border-strong:   #d4d4d8;

  --color-text-primary:    #1a1a1a;
  --color-text-secondary:  #5d5d66;
  --color-text-tertiary:   #8e8e99;

  /* ── 强调色（品牌紫，仅三处使用） ── */
  --color-accent:          #7c3aed;
  --color-accent-hover:    #6d28d9;
  --color-accent-soft:     #f4f0ff;
  --color-accent-fg:       #ffffff;

  /* ── 深色模式 ── */
  --color-surface-dark:        #1a1a1c;
  --color-surface-sunken-dark: #0f0f11;
  --color-border-subtle-dark:  #2e2e33;

  /* ── 尺度 ── */
  --sidebar-w:      16rem;    /* 260px 级；合并后的唯一侧栏 */
  --topbar-h:       3rem;
  --content-max:    48rem;    /* 正文列上限，中文可读性拐点 */
  --panel-w:        26rem;    /* Artifact 面板并排时的宽度 */

  --radius-composer: 1.5rem;
  --radius-bubble:   1rem;
  --radius-control:  0.625rem;

  /* ── 唯一的两级阴影 ── */
  --shadow-subtle: 0 1px 2px rgba(0,0,0,.04);
  --shadow-panel:  0 1px 3px rgba(0,0,0,.04), 0 8px 24px -16px rgba(0,0,0,.12);

  /* ── 容器查询断点（语义命名，见 §4.2） ── */
  --container-compact: 30rem;   /* 480px */
  --container-cozy:    40rem;   /* 640px */
  --container-wide:    52rem;   /* 832px */
}
```

**移除清单**：`--mobile-chat-height`、`.glass-panel`、`.hover-lift`、`.glow-shadow-*`（4 个）、`.animate-float`、`.animate-pulse-glow`、`body.ne-chat-active`、`.pb-mobile-nav`、`.bottom-mobile-nav`、`.ne-empty-logo` 及其两条 `max-height` 媒体查询。

`--mobile-nav-height` 在底部 tab bar 删除后一并移除。

---

## 4. 断点体系

### 4.1 视口断点：回归 Tailwind 标准，删除所有自定义值

`min-[1360px]` 与 `min-[1680px]` 全量废弃，映射如下：

| 断点 | 宽度 | 侧栏默认态 | 内容列上限 | Artifact 面板 | 顶栏 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| base | <640 | overlay | 100% − 2rem | 全屏覆盖 | 汉堡 + 标题 |
| `sm` | ≥640 | overlay | 100% − 3rem | 全屏覆盖 | 汉堡 + 标题 |
| `md` | ≥768 | overlay | 42rem | 覆盖 | 汉堡 + 标题 |
| `lg` | ≥1024 | **展开（push）** | 44rem | 覆盖 | 折叠钮 + 标题 |
| `xl` | ≥1280 | 展开 | 46rem | 覆盖 | 折叠钮 + 标题 |
| `2xl` | ≥1536 | 展开 | 48rem | **并排推挤** | 折叠钮 + 标题 |

**`lg`(1024) 取代原来的 1360 是修复死区的关键一步**：iPad 横屏、半屏窗口从此拿到完整桌面骨架。

原 `min-[1680px]` 的间距放大档映射到 `xl:` / `2xl:`。

内容列上限从当前的 `max-w-5xl`（1024px）收到 48rem（768px）。中文正文每行超过约 40 字后阅读效率显著下降。

### 4.2 容器断点：组件内部一律用容器查询

视口断点**无法描述内容区实际宽度**。同样是 1280px 视口：侧栏展开时内容区 1020px，侧栏收起时 1280px，再开 Artifact 面板只剩 604px——三者视口断点完全一致，布局需求却完全不同。这是当前 ChatBox 工具栏在某些尺寸挤成一团的直接原因。

项目已是 Tailwind v4（`tailwindcss ^4.2.2`），容器查询内置可用。约定：

- `<main>` 声明为命名容器 `@container/main`
- 组件内使用 `@compact/main:` / `@cozy/main:` / `@wide/main:` 三档（对应 §3 中定义的 480 / 640 / 832px）

```html
<main class="@container/main flex min-h-0 flex-col">
  <div class="flex items-center gap-1 @cozy/main:gap-2 @wide/main:gap-3">
    <button class="...">
      <Globe class="size-4" />
      <span class="hidden @cozy/main:inline">联网</span>
    </button>
  </div>
</main>
```

**替换规则**：ChatBox 中所有 `hidden sm:inline`（`联网` / `技能` / `知识库` / 模型名的文字标签）改为 `hidden @cozy/main:inline`；所有 `sm:hidden` 的角标改为 `@cozy/main:hidden`。这样在任意「侧栏 × 面板 × 视口」组合下自动做对，无需为组合数爆炸的场景各写断点。

`ArtifactSidebar` 与 `HistorySidebar` 各自也应声明为容器（`@container/panel`、`@container/side`），内部按自身宽度决策。

---

## 5. 布局架构

### 5.1 骨架

```
┌─ AppShell  grid: [sidebar] [main] [panel] ─────────────────┐
│ ┌─ AppSidebar ─┐ ┌─ Main ─────────────────┐ ┌─ Panel ────┐ │
│ │  品牌         │ │ TopBar  sticky 3rem    │ │ Artifact   │ │
│ │  新建会话     │ │ ┌────────────────────┐ │ │            │ │
│ │  ───────────  │ │ │ ScrollArea flex-1  │ │ │  ≥2xl 并排 │ │
│ │  导航入口     │ │ │  内容列 居中        │ │ │  <2xl 覆盖 │ │
│ │  ───────────  │ │ │  max-w 见 §4.1     │ │ │            │ │
│ │  会话列表     │ │ └────────────────────┘ │ │            │ │
│ │  flex-1 滚动  │ │ Composer sticky bottom │ │            │ │
│ │  用户区       │ │  shrink-0 文档流内     │ │            │ │
│ └──────────────┘ └────────────────────────┘ └────────────┘ │
└────────────────────────────────────────────────────────────┘
```

三个关键改变：

**（a）侧栏合并。** 当前 NavRail(76px) + HistorySidebar(300px) 是两条独立柱子。合并为单个 `--sidebar-w`(16rem) 侧栏，自上而下：品牌 → 新建会话 → 导航入口（各端不同，见 §8）→ 会话列表（`flex-1` 滚动）→ 用户区。合并后 <1024px 的底部五宫格 tab bar 可直接删除，导航已在抽屉内。

**（b）侧栏三态，由状态驱动而非断点驱动。**

| 状态 | 行为 |
| :--- | :--- |
| `expanded` | 占据 grid 首列，推开内容 |
| `collapsed` | 列宽 0，内容占满；顶栏显示展开钮 |
| `overlay` | 脱离 grid，`fixed` 浮层 + 遮罩 |

断点只决定**初始默认值**（<1024 为 `overlay`，≥1024 为 `expanded`）。用户手动切换后写入 `localStorage.sidebar_state` 并被尊重——这正是当前 `ui_mode` 想做、但用换路由做坏了的事。

**（c）Composer 回到文档流。** Main 为 `flex-col`；ScrollArea `flex-1 min-h-0 overflow-y-auto`；Composer 是它的兄弟节点 `shrink-0 sticky bottom-0`。输入框长多高，滚动区自然缩多少，结构上不可能遮挡。`--mobile-chat-height` 整个删除。

### 5.2 AppShell 代码骨架

```vue
<!-- src/components/layout/AppShell.vue -->
<script setup lang="ts">
import { useSidebar } from '@/composables/useSidebar';

const { state, isOverlay, toggle, close } = useSidebar();
</script>

<template>
  <div
    class="app-shell grid h-[100dvh] overflow-hidden bg-surface text-text-primary"
    :data-sidebar="state"
  >
    <AppSidebar class="app-shell__sidebar" @navigate="isOverlay && close()" />

    <Transition name="fade">
      <div
        v-if="isOverlay && state === 'expanded'"
        class="fixed inset-0 z-40 bg-black/40 lg:hidden"
        @click="close"
      />
    </Transition>

    <main class="@container/main flex min-h-0 min-w-0 flex-col">
      <TopBar @toggle-sidebar="toggle" />
      <slot />
    </main>

    <ArtifactPanel class="app-shell__panel" />
  </div>
</template>

<style scoped>
.app-shell {
  grid-template-columns: var(--col-sidebar, 0px) minmax(0, 1fr) var(--col-panel, 0px);
  transition: grid-template-columns 200ms ease;
}
.app-shell[data-sidebar='expanded'] {
  --col-sidebar: var(--sidebar-w);
}
.app-shell[data-panel='docked'] {
  --col-panel: var(--panel-w);
}

/* overlay 模式：侧栏脱离栅格，栅格首列归零 */
@media (width < 64rem) {
  .app-shell { --col-sidebar: 0px !important; }
  .app-shell__sidebar {
    position: fixed;
    inset-block: 0;
    inset-inline-start: 0;
    z-index: 50;
    width: var(--sidebar-w);
    transform: translateX(-100%);
    transition: transform 200ms ease;
  }
  .app-shell[data-sidebar='expanded'] .app-shell__sidebar {
    transform: translateX(0);
  }
}

/* <2xl：Artifact 面板覆盖而非推挤 */
@media (width < 96rem) {
  .app-shell { --col-panel: 0px !important; }
  .app-shell__panel { position: fixed; inset-block: 0; inset-inline-end: 0; z-index: 45; }
}

@media (prefers-reduced-motion: reduce) {
  .app-shell,
  .app-shell__sidebar { transition: none; }
}
</style>
```

### 5.3 Main 内部（ChatView）

```vue
<template>
  <!-- 滚动区与 Composer 是兄弟节点，无 fixed、无避让常量 -->
  <div
    ref="scrollArea"
    class="flex min-h-0 flex-1 flex-col overflow-y-auto overscroll-contain"
    :class="hasMessages ? 'justify-start' : 'justify-center'"
  >
    <MessageList v-if="hasMessages" :messages="messages" />
    <EmptyState v-else @pick="onPickSuggestion" />
  </div>

  <div class="shrink-0 bg-surface px-4 pb-3 pt-2 @cozy/main:px-6 @cozy/main:pb-4">
    <div class="mx-auto w-full max-w-(--content-max)">
      <Composer v-bind="composerProps" @send="onSend" />
    </div>
  </div>
</template>
```

空态与常态的切换只靠 `justify-center` ↔ `justify-start`，零 JS 测量。与 ChatGPT 行为一致：无消息时 greeting 与输入框视觉居中，首条消息发出后输入框沉底。

### 5.4 纵向自适应

三条规则替换全部 `max-height` 媒体查询：

1. 外壳用 `h-[100dvh]` 而非 `h-screen`；`index.html` 的 viewport meta 追加 `interactive-widget=resizes-content`，解决移动端键盘弹起顶飞布局：

```html
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover, interactive-widget=resizes-content" />
```

2. 空态 logo 与标题用 `clamp()` 连续缩放，替代 `10vh / 7.5vh / 17vh` 三档跳变：

```html
<img class="w-auto object-contain" style="height: clamp(2.5rem, 7vh, 6rem)" />
<h2 class="font-semibold" style="font-size: clamp(1.125rem, 2.2vh, 1.5rem)">…</h2>
```

3. 空态容器本身允许滚动。1280×720 这类矮屏出现滚动条是正确行为，优于压缩到看不清。

配合此项，删除 `body.ne-chat-active { overflow: hidden }` 与路由/组件里全部 10 次 `setTimeout(scrollTo)`。外壳 `h-[100dvh] overflow-hidden` + 内部唯一滚动容器，结构上不可能产生 body 滚动。

---

## 6. 组件规范

### 6.1 AppSidebar（新建，合并 NavRail + HistorySidebar）

```
padding      : 0.75rem
背景          : --color-surface-sunken
右边框        : 1px --color-border-subtle
品牌区        : 高 3rem，与 TopBar 对齐；logo 24px + 名称 14px/600
新建会话      : 全宽，高 2.5rem，radius --radius-control
                背景 --color-accent，文字 --color-accent-fg（强调色使用点 1/3）
导航入口      : 行高 2.25rem，图标 18px + 文字 14px
                激活态背景 --color-accent-soft，文字 --color-accent（强调色使用点 2/3）
分组标题      : 12px/500 --color-text-tertiary，上间距 1rem
会话项        : 行高 2.25rem，radius 0.5rem，单行截断
                hover 背景 --color-surface-hover，右侧浮出重命名/删除
                激活态同导航入口
用户区        : 上边框分隔，头像 32px 圆形 + 名称 + 状态点
```

移除：渐变按钮、`shadow-[0_18px_34px_-18px_rgba(124,58,237,0.9)]`、`backdrop-blur-xl`、`rounded-2xl` 大圆角、用户区的嵌套卡片（改为扁平行）。

### 6.2 TopBar

高 `--topbar-h`(3rem)，`sticky top-0`，底边 1px 分隔线，背景 `--color-surface`（不做毛玻璃）。

左侧：侧栏切换钮（`overlay` 态显示汉堡，`collapsed` 态显示展开钮，`expanded` 态显示收起钮）+ 当前智能体名称。右侧：Artifact 面板切换钮。

删除当前顶栏的品牌渐变方块、`专家模式` / `访客模式` 徽章（移到侧栏用户区）。

### 6.3 Composer

```
容器      : max-width --content-max，居中
外观      : 1px --color-border-strong，radius --radius-composer
            背景 --color-surface，聚焦时 --shadow-panel + 边框转 --color-accent
            无渐变、无 backdrop-blur
textarea  : min-height 3rem，自增至 max-height 12rem 后内部滚动
工具栏     : 单行，flex，gap 0.25rem
            左：附件 / 联网 / 技能 / 知识库（文字标签按 @cozy/main 显隐）
            右：模型选择 + 发送钮（40px 圆形，背景 --color-accent，强调色使用点 3/3）
附件区     : textarea 上方，横向滚动 chip 行
```

关键：工具栏必须 `min-w-0` + 左组 `overflow-x-auto scrollbar-none`，右组 `shrink-0`。当前 `flex-nowrap` 无溢出容器，窄容器下会把发送钮挤出边界。

### 6.4 MessageList

```
容器       : max-width --content-max，居中，px 1rem / @cozy 1.5rem
消息间距    : 1.5rem / @wide 2rem
用户消息    : 右对齐气泡，max-width 80%，背景 --color-surface-sunken
            radius --radius-bubble，无阴影无边框
助手消息    : 全宽，无气泡无卡片（当前的 rounded-[32px] + 双层阴影卡片全部去掉）
            仅头像 + 正文，靠留白分隔
操作条      : hover 时浮现于消息下方，图标 16px，--color-text-tertiary
```

助手消息去卡片化是与 ChatGPT 对齐的核心视觉改动，也顺带解决了窄容器下卡片内边距吃掉过多宽度的问题。

### 6.5 EmptyState

当前 11 个建议 chip 在窄屏会堆成一大片。改为：

```
logo      : clamp(2.5rem, 7vh, 6rem)
greeting  : clamp(1.125rem, 2.2vh, 1.5rem)，600 字重
副标题     : 14px --color-text-secondary
建议 chip : 最多 4 个（移动端 3 个）
            布局 grid-cols-1 @cozy/main:grid-cols-2
            单个：左对齐，1px 边框，radius 0.75rem，padding 0.75rem 1rem
```

移除四个装饰圆环与外壳的径向渐变光晕。

### 6.6 ArtifactPanel

`≥2xl` 并排推挤（占 grid 第三列，宽 `--panel-w`）；`<2xl` 覆盖式，宽 `min(26rem, 100vw)`，带遮罩；`<md` 全屏。内部声明 `@container/panel`，代码预览区按自身宽度决定是否显示行号。

---

## 7. 路由改造

删除 `/m` 分支，所有 `/m/*` 永久重定向到对应 `/app/*`。

```ts
// 移除 detectDefaultMode、remapChatPathByMode
// 移除 beforeEach 中的 isChatPath / expectedMode 整段重定向逻辑
// 移除 afterEach 与 beforeEach 中全部 scrollTo 调用

const routes = [
  { path: '/', redirect: '/app' },
  { path: '/app/:threadId?', component: () => import('@/views/ne-full/index.vue'), meta: { title: APP_TITLE } },

  // 兼容旧链接与收藏
  { path: '/m/:threadId?',        redirect: (to) => ({ path: `/app/${to.params.threadId ?? ''}`.replace(/\/$/, '') }) },
  { path: '/m/explore',           redirect: '/explore' },
  { path: '/m/settings',          redirect: '/settings' },
  { path: '/chat/:threadId?',     redirect: (to) => ({ path: `/app/${to.params.threadId ?? ''}`.replace(/\/$/, '') }) },
  { path: '/ne-full/:threadId?',  redirect: (to) => ({ path: `/app/${to.params.threadId ?? ''}`.replace(/\/$/, '') }) },
];
```

各端还需按 §8 处理自己独有的 `/m/*` 路径（RadAI 的 `/m/custom-learning`、`/m/custom-learning/path/:planId?`、`/m/learning-style`）。

同步改造：

- `useUiMode.ts` — 删除。`ui_mode` 语义由 `sidebar_state` 取代。
- `useDeviceProfile.ts` — 保留 `isTouch` 能力探测（用于 hover 态降级），删除 `detectMode` 与 900px 阈值。
- `navigation.ts` — `NavItem.match` 中的 `/m` 项全部移除，`isNavItemActive` 里的 `/m` 特判整段删除，函数可简化为纯前缀匹配。
- `ne-full/index.vue` — 删除 `isMobileLayout`、`getTargetChatPath`、`syncLayoutRoute`、`watch(effectiveMode)`、`showMobileSidebar` 及移动抽屉那段 `Transition`（抽屉逻辑上移到 AppShell）。

新增 `composables/useSidebar.ts`：

```ts
type SidebarState = 'expanded' | 'collapsed';

const KEY = 'sidebar_state';
const DESKTOP_QUERY = '(width >= 64rem)';

export const useSidebar = () => {
  const mql = window.matchMedia(DESKTOP_QUERY);
  const isDesktop = ref(mql.matches);
  const stored = localStorage.getItem(KEY) as SidebarState | null;
  const state = ref<SidebarState>(stored ?? (mql.matches ? 'expanded' : 'collapsed'));

  const onChange = (e: MediaQueryListEvent) => {
    isDesktop.value = e.matches;
    // 用户未显式表态时才跟随断点；已表态则尊重用户选择
    if (!localStorage.getItem(KEY)) state.value = e.matches ? 'expanded' : 'collapsed';
  };
  mql.addEventListener('change', onChange);
  onBeforeUnmount(() => mql.removeEventListener('change', onChange));

  const isOverlay = computed(() => !isDesktop.value);
  const toggle = () => {
    state.value = state.value === 'expanded' ? 'collapsed' : 'expanded';
    localStorage.setItem(KEY, state.value);
  };
  const close = () => { state.value = 'collapsed'; };

  return { state, isDesktop, isOverlay, toggle, close };
};
```

注意 `matchMedia` 只在断点跨越时触发一次，不会像 `resize` 监听那样每帧刷新，也不会引起重挂载。

---

## 8. 三端落地差异

本轮三个前端各自独立改，规范一致，差异如下。

| 项 | `frontend/`（RadAI） | `frontend_csh_ai/`（CSH-AI） | `frontend_fmca/`（FMCA） |
| :--- | :--- | :--- | :--- |
| 端口 | 5173 | 5174 | 5176 |
| 侧栏导航项 | 知识问答 / 探索式学习 / 自定义学习 / 学习风格 | 知识问答 / 探索式学习 / 地下水项目 | 知识问答 / 探索式学习 |
| 额外 `/m` 重定向 | `/m/custom-learning`、`/m/custom-learning/path/:planId?`、`/m/learning-style` | 无 | 无 |
| 额外顶层路由 | `/custom-learning`、`/learning-style` | `/groundwater/:threadId?` | 无 |
| 项目选择条 | 无 | `projectMode` 下的项目下拉条，需并入 TopBar 下方独立行，`@compact/main` 以下折叠为图标按钮 | 无 |
| 知识库入口 | 有 | 有 | 无（`kb_policy: disabled`，Composer 隐藏知识库钮） |
| 品牌 | RadAI logo / 主色沿用 `--color-accent` | 成理助手 logo | 基础力学 logo |

> **`frontend/` 改动的连带影响**：`guest/`（访客端，:8085）通过 Vite alias `@ → ../frontend/src` 复用 `ne-full` 的组件与 composable。改 `frontend/src` 会直接影响访客端。`guest/` 无 Vue Router，自行管理登录/对话/历史状态，**不能使用依赖路由的 `useSidebar` 与 AppShell**。
>
> 处理方式：`AppShell.vue`、`AppSidebar.vue`、`useSidebar.ts` 全部做到不依赖 `vue-router`（导航项通过 props 传入、点击派发事件而非 `router.push`），由 `frontend/` 的 `ne-full/index.vue` 与 `guest/src/App.vue` 各自接线。`frontend/` 改完后必须回归 guest 端。

---

## 9. 实施任务清单

每个阶段结束都应是可运行、可回滚的状态。以下清单需在三个前端各执行一遍。

### P1 · 布局地基（解决遮挡与死区，见效最快）

- [x] `index.html` viewport meta 增加 `interactive-widget=resizes-content, viewport-fit=cover`（三端）
- [x] 新增 `composables/useSidebar.ts`
- [x] 新增 `components/layout/AppShell.vue`、`AppSidebar.vue`、`TopBar.vue`
- [x] `AppSidebar` 合并 `NavRail.vue` 与 `HistorySidebar.vue`，删除底部 tab bar 的 `Teleport` 块
- [x] `ne-full/index.vue` 改为 AppShell 的 slot 内容；Composer 移入文档流 `sticky bottom-0`
- [x] 删除 `--mobile-chat-height`、`--mobile-nav-height`、`.pb-mobile-nav`、`.bottom-mobile-nav`
- [x] 删除 `body.ne-chat-active` 与全部路由级 `setTimeout(scrollTo)`
- [x] 路由：删 `/m` 分支、加重定向、删 `detectDefaultMode` / `remapChatPathByMode`（三端）
- [x] 删除 `useUiMode.ts`、`useDeviceProfile.ts`；清理 `navigation.ts` 的 `/m` 特判
- [x] `frontend/` 完成后回归 `guest/`（token + `@container/main`；guest 不接 AppShell）

### P2 · 断点与容器查询

- [x] 全量替换 `min-[1360px]` → `lg:`（对话主路径）
- [x] 全量替换 `min-[1680px]` → `xl:` / `2xl:` / 容器查询（对话主路径）
- [x] `<main>` 加 `@container/main`；ArtifactPanel、AppSidebar 各自加命名容器
- [x] ChatBox 工具栏：`hidden sm:inline` → `hidden @cozy/main:inline`；左组加 `min-w-0 overflow-x-auto scrollbar-none`，右组 `shrink-0`
- [x] MessageList 内容列 `max-w-5xl` → `max-w-(--content-max)`
- [x] 删除对话空态 `@media(max-height:900px)` 与 `.ne-empty-logo`，改 `clamp()`
- [x] ArtifactPanel 实现 `≥2xl` 并排 / `<2xl` 覆盖 / `<md` 全屏三态

### P3 · 视觉重做

- [x] 替换 `style.css` 的 `@theme` 为 §3 token 集
- [x] 删除 `.glass-panel` / `.hover-lift` / `.glow-shadow-*` / `.animate-float` / `.animate-pulse-glow`（`style.css` 定义已清；admin 等处仍有死类名残留）
- [x] 外壳移除径向渐变光晕与四个装饰圆环（对话空态）
- [x] AppSidebar、TopBar、Composer、MessageList 按 §6 重做视觉
- [x] 助手消息去卡片化（移除 `rounded-[32px]` + 双层阴影）
- [x] EmptyState 建议 chip 收敛到 4 个，改 grid 布局
- [x] 深色模式：面色改 token、反色按钮与紫底浅字等错配已修；少量成对 `dark:bg-[#…]` 仍保留

### P4 · 验收

见 §10。`frontend_csh_ai` 已跑通十档尺寸与交互；`frontend` / `frontend_fmca` 以类型检查 + 构建 + 关键页深色对比度抽查为主。

---

## 10. 验收清单

必须**用拖动窗口的方式连续改变尺寸**逐一验证，而不只是在每个尺寸刷新——当前架构的核心缺陷恰恰只在连续变化时暴露。

尺寸矩阵：

| 尺寸 | 场景 |
| :--- | :--- |
| 360×640 | 小屏安卓 |
| 390×844 | iPhone，**含键盘弹起** |
| 768×1024 | iPad 竖屏 |
| 960×1080 | 1080p 半屏窗口（原死区） |
| 1024×768 | 小笔记本（原死区） |
| 1180×820 | iPad 横屏（原死区） |
| 1280×720 | 矮屏笔记本 |
| 1440×900 | MacBook |
| 1920×1080 | 主流桌面 |
| 2560×1440 | 2K，验证 `2xl` 并排面板 |

每个尺寸检查六项：

1. 输入框不遮挡最后一条消息（**打满 8 行文字 + 挂 3 个附件后复测**）
2. Composer 工具栏不换行、不溢出，发送钮始终可见
3. 侧栏状态符合默认预期；手动切换后跨路由跳转与刷新仍被记住
4. 浏览器缩放 200% 时无横向滚动条
5. 拖动窗口跨越 1024px 与 1536px 时无组件重挂载（DevTools 观察，不应出现闪烁或状态丢失）
6. 无 body 级滚动（`document.scrollingElement.scrollTop` 恒为 0）

补充验证：
- Artifact 面板打开状态下重跑 1024 / 1280 / 1536 三档
- `prefers-reduced-motion: reduce` 下过渡关闭
- 深色模式全尺寸抽查

---

## 十一、实施记录（frontend_csh_ai 已落地）

### 11.1 变更清单

新增：

| 文件 | 作用 |
| --- | --- |
| `src/components/layout/AppShell.vue` | 三列栅格骨架，唯一的 `100dvh` 根容器 |
| `src/components/layout/AppSidebar.vue` | 合并原 NavRail 与 HistorySidebar |
| `src/components/layout/TopBar.vue` | 统一顶栏与侧栏开关 |
| `src/composables/useSidebar.ts` | 侧栏三态与 docked/overlay 判定 |
| `src/composables/useMediaQuery.ts` | `matchMedia` 封装，替代 resize 监听 |

删除：`NavRail.vue`、`HistorySidebar.vue`、`useUiMode.ts`、`useDeviceProfile.ts`。

改造：`style.css`（新 token + `@custom-variant dark`）、`router/index.ts`（`/m` 降级为重定向）、`navigation.ts`、`ne-full/index.vue`、`MessageList.vue`、`ChatBox.vue`、`ArtifactSidebar.vue`、`explore/index.vue`、`settings/index.vue`、`MyFilesView.vue`、`index.html`（viewport 加 `viewport-fit=cover, interactive-widget=resizes-content`）。

被移除的 resize 监听共三处：`useUiMode`（驱动路由跳转）、`MessageList`（决定建议条数量）、`ChatBox`（决定菜单弹出方式）。前两者改为纯 CSS，后者改为 `matchMedia`。

### 11.2 两个必须记住的 CSS 陷阱

实施中踩到两个坑，都会静默产生错误布局而不报错：

**一、`position: fixed` 的子元素不占栅格单元。** 侧栏在窄视口下变成抽屉后脱离栅格流，栅格里只剩 `main` 一个 in-flow 子元素，于是它被自动放进第一列（宽 0），页面整片空白。必须给每个子元素显式锁定列号：

```css
.app-shell__sidebar { grid-column: 1; grid-row: 1; }
.app-shell__main    { grid-column: 2; grid-row: 1; }
```

工件面板同理，用 Tailwind 的 `col-start-3 row-start-1` 锁定。

**二、媒体查询不提升选择器特异性。** 原写法是先给 `.app-shell[data-sidebar='expanded']` 赋列宽，再在窄屏媒体查询里用 `.app-shell` 归零。前者特异性 `(0,2,0)` 压过后者的 `(0,1,0)`，媒体查询完全不生效——窄屏打开抽屉时主区被挤到 134px。正确做法是**只在目标断点内赋值**，而不是先赋值再归零：

```css
@media (width >= 64rem) {
  .app-shell[data-sidebar='expanded'] { --col-sidebar: var(--sidebar-w); }
}
```

### 11.3 验证结果

用 Playwright 在 dev server 上跑了三轮自动化断言，全部通过：

- **十档尺寸**（360×640 到 2560×1440）：无 body 滚动、无横向溢出、输入框不溢出右边界、滚动区与输入框零重叠。原死区 960/1024/1180 现在都是完整桌面骨架。
- **交互**：窄屏抽屉滑入不推挤主区、点遮罩关闭、长文本使输入框增高 121→265px 且滚动区同步收缩、2560px 工件面板并排推挤（`static`）而 1440px 覆盖不推挤（`fixed`）、深色模式 `.dark` class 与 token 覆写均生效、200% 缩放无横向滚动条。
- **真实对话**：390 与 1440 各发一条消息，Markdown 表格在 390 宽下正常折行不溢出。

### 11.4 三端与 guest 状态（2026-07-25 更新）

| 端 | 状态 | 备注 |
| --- | --- | --- |
| `frontend_csh_ai` | 已落地并验收 | 十档尺寸 + 交互 + 对话流 |
| `frontend` | 已落地 | AppShell / 路由 / token / viewport；guest 联动 token |
| `frontend_fmca` | 已落地 | 路由已去 `/m` 双轨；导航仅问答 + 探索 |
| `guest` | 兼容 | 不接 AppShell；`style.css` 同步 token，`main` 加 `@container/main` |

已知残留（不阻塞主路径）：

1. `AppSidebar` 仍直接 `import vue-router`（guest 未使用该组件，功能可接受）。
2. admin / 登录等页仍有少量硬编码 hex、死类名（`hover-lift` 等）与「强调色仅三处」未严格覆盖。
3. `useDeviceProfile` 整文件删除（文档曾要求保留 `isTouch`）；当前无引用。

---

© 2026 智能体平台前端
