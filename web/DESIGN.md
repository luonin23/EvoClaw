# EvoClaw Fuselab-Creative 设计系统

> 基于 Fuselab Creative 官网（https://fuselabcreative.com/）视觉风格复刻。当前实现为**深色主题**，`index.html`（交易终端）和 `intro.html`（落地页）均使用同一套深色 tokens，以保证视觉统一。

## Visual Theme & Atmosphere

- **Mood**: professional_minimal + editorial
- **Feel**: "Technical precision meets creative confidence" —— 干净、自信、克制，带有数据/AI 产品的精密感
- **References**: Fuselab Creative agency site, enterprise AI dashboards
- **核心视觉特征**:
  - 大面积留黑
  - 大字号标题，低字重
  - 高对比度
  - 单一点亮绿 accent，使用克制
  - 卡片式信息组织
  - 圆角柔和但不过度
  - 背景氛围动画（aurora 光晕 + 流动网格）
  - 数据可视化动效（数字 flash、图表入场、矩阵 pulse）

## Color Palette & Roles

当前 `index.html` 和 `intro.html` 均使用以下**深色主题** tokens。

| Token | Hex | Role |
|-------|-----|------|
| `--bg-base` | `#0a0a0a` | 页面背景 |
| `--bg-elevated` | `#141414` | 卡片/面板背景 |
| `--bg-surface` | `#1a1a1a` | elevated surface |
| `--bg-hover` | `#242424` | 悬停背景 |
| `--text-primary` | `#f5f5f5` | 主文字 |
| `--text-secondary` | `#a1a1aa` | 次要文字 |
| `--text-muted` | `#71717a` | 弱化文字 |
| `--accent` | `#00d084` | 强调色（亮绿） |
| `--accent-hover` | `#00b86b` | 强调色悬停 |
| `--success` | `#00d084` | 盈利/做多 |
| `--error` | `#ff4757` | 亏损/做空 |
| `--warning` | `#f59e0b` | 警告 |
| `--border-subtle` | `rgba(255,255,255,0.08)` | subtle 边框 |
| `--border-medium` | `rgba(255,255,255,0.12)` | 中等边框 |
| `--border-strong` | `rgba(255,255,255,0.16)` | 强边框 |

### 迁移映射（旧 → 新）

| 旧 Token | 旧值 | 新 Token | 新值 |
|----------|------|----------|------|
| `--bg-base` | `#070a12` | `--bg-base` | `#0a0a0a` |
| `--bg-elevated` | `#0d1220` | `--bg-elevated` | `#141414` |
| `--bg-surface` | `#111827` | `--bg-surface` | `#1a1a1a` |
| `--accent` | `#00d4ff` | `--accent` | `#00d084` |
| `--success` | `#00e5a0` | `--success` | `#00d084` |
| `--error` | `#ff4757` | `--error` | `#ff4757`（保留） |

## Typography Rules

- **Display**: `Inter`, 500, `clamp(2.5rem, 5vw, 4rem)`, letter-spacing `-0.02em`, line-height 1.1
- **Body**: `Inter`, 400, `1rem/1.6`
- **Mono**: `JetBrains Mono`, 400, `0.875rem`（保留等宽字体用于数据）
- **UI Labels / Captions**: `Inter`, 500, `0.875rem`, letter-spacing `0.01em`
- **Hero Title（intro）**: `clamp(3rem, 8vw, 6rem)`, weight 500, line-height 1.05

## Component Stylings

### Buttons

- **Primary**: accent bg (`#00d084`), dark text (`#0a0a0a`), rounded-full (pill), padding `12px 24px`, font-weight 500
- **Secondary**: transparent bg, `1px solid border-medium`, text-primary, rounded-full, hover bg-hover
- **Danger**: error bg, white text, rounded-full

### Cards

- bg-surface
- border-radius: `20px`
- border: `1px solid var(--border-subtle)`
- padding: `24px`
- hover: subtle `translateY(-2px)` + soft shadow, **不**使用 scale 动画

### Inputs

- bg: `var(--bg-base)`
- border: `1px solid var(--border-medium)`
- border-radius: `12px`
- focus: `2px solid var(--accent)` outline, **不**使用默认浏览器 glow

### Tabs

- pill-style tab buttons
- active: accent bg + dark text
- inactive: transparent + text-secondary

### Matrix Cells

- border-radius: `8px`（小格子 `6px`）
- 保留 HSL 热力图逻辑
- 方向边框：多单 accent green，空单 error red
- 入场动画：stagger 缩放淡入
- 盈利/亏损格子：idle pulse glow 动画
- hover: 柔和 glow + 放大

### Motion

- **Background**: aurora gradient drift (20s) + flowing grid (30s)
- **Data value flash**: scale(1.05) + color flash on update
- **Chart**: fade-in/scale entrance
- **Cards/Buttons**: translateY(-2px) + accent glow on hover
- ** prefers-reduced-motion**: all animations respect reduced motion

## Layout Principles

- **Max width**: `1280px`
- **Section spacing**: `120px` desktop / `80px` mobile
- **Content padding**: `48px` desktop / `24px` mobile
- **Grid**: CSS Grid for 2D layouts, Flexbox for 1D
- **Responsive grids**: `repeat(auto-fit, minmax(280px, 1fr))`
- **Body text max-width**: `65ch`

## Depth & Elevation

- **Shadows**: 使用 subtle, diffused shadows，不使用硬阴影
  - card: `0 4px 24px rgba(0,0,0,0.06)` (light) / `0 4px 24px rgba(0,0,0,0.25)` (dark)
- **Borders**: 1px solid with low opacity，不使用彩色侧边条
- **Glassmorphism**: 仅在锁屏覆盖层等必要场景使用，且保持克制

## Do's and Don'ts

- DO 使用声明的 color tokens exclusively
- DO 保持大字号、宽松的 section spacing
- DO 确保所有文字 WCAG AA 对比度
- DO 为所有动画提供 `prefers-reduced-motion` 降级
- DON'T 使用 gradient text
- DON'T 使用 glassmorphism 作为默认卡片样式
- DON'T 在卡片内嵌套卡片
- DON'T 使用超过 2 种主要字体
- DON'T 使用 bounce / elastic easing
- DON'T 对 `<img>` 做 hover scale/rotate

## Responsive Behavior

- **Breakpoints**: 640px (sm), 768px (md), 1024px (lg), 1280px (xl)
- **Mobile**: 单列，堆叠所有 sections，减小标题字号
- **Tablet**: 允许 2-column grids
- **Desktop**: 完整布局 + max-width 约束
- **Images**: fluid, max-width 100%, maintain aspect ratio

## Agent Prompt Guide

- 不要发明 palette 之外的颜色
- 不要添加未在 Depth & Elevation 中声明的 box-shadow
- accent 色在每个视口中最多出现 3 次主要元素
- 所有交互元素需要 `:focus-visible` outline
- 不要修改任何 JavaScript 逻辑（API 调用、图表绘制、矩阵算法、密码锁）
- 同步 Canvas 图表颜色到新 tokens
- 两个 HTML 文件共享 `:root` tokens，保持同步

## Migration Notes

1. `index.html` 和 `intro.html` 的 `:root` 使用同一套深色 token 值
2. Canvas `drawChart()` 中硬编码颜色已替换为新 tokens
3. `generatePoster()` 的 `backgroundColor` 已同步新 `--bg-base`
4. 保留现有 JS 函数签名和 DOM 结构
5. 所有 hover/entrance 动画使用 `ease-out` 或 `cubic-bezier(0.16, 1, 0.3, 1)`
6. 持仓矩阵支持滚动，超出视口时可上下滚动查看
