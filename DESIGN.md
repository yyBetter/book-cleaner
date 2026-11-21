# Design System · 超级扫描仪（SuperScanner）

> Approved Direction: **方案 A · 「Apple Studio 极净白纸工坊」**
> Benchmarks: Apple Craft · Things 3 · Photomator Light · macOS Sequoia Native Studio Utilities

## 1. Visual Identity & Surfaces

The workspace centers physical book pages inside a calm, bright alabaster studio mat so paper edges, lighting cast, and before/after bleaching contrast read clearly without screen glare or bordered-box clutter.

### Default Light Theme (`[data-theme="light"]`, default)
- `--bg`: `#F5F5F3` — warm alabaster outer shell
- `--canvas`: `#ECECE9` — quiet gallery mat behind the live viewfinder and before/after comparison stage
- `--surface`: `#FFFFFF` — pure white topbar, borderless right inspector, workspace cards, and dialogs
- `--surface-rail`: `#FBFBF9` — feather-light alabaster left navigation rail
- `--surface-subtle`: `#F4F4F1` — soft pill backgrounds, ghost fields, hover fills
- `--surface-glass`: `rgba(255, 255, 255, 0.86)` — frosted floating shutter capsule and mobile camera HUD (`backdrop-filter: blur(18px) saturate(160%)`)
- `--border`: `rgba(24, 24, 27, 0.08)` — whisper-thin architectural dividers
- `--border-strong`: `rgba(24, 24, 27, 0.16)` — hover and interactive borders
- `--text`: `#18181B` — primary ink charcoal
- `--muted`: `#6E6E77` — secondary labels and metadata
- `--subtle`: `#8F8F98` — quiet captions and separators
- `--primary`: `#18181B` (`--primary-hover`: `#27272A`, `--primary-text`: `#FFFFFF`) — signature restrained graphite-black CTA (`拍照入库`, `通过`, primary submits)
- `--switch-off`: `#D4D4D8`, `--switch-on`: `#3F3F46` — quiet monochrome switches (no loud neon blue)
- `--ok`: `#047857`, `--ok-dot`: `#10B981`, `--ok-tint`: `rgba(16, 185, 129, 0.10)`
- `--warn`: `#B45309`, `--warn-tint`: `rgba(217, 119, 6, 0.10)`
- `--danger`: `#DC2626`, `--danger-tint`: `rgba(220, 38, 38, 0.09)`

### Neutral Obsidian Dark Counterpart (`[data-theme="dark"]`, optional)
- Zero-blue neutral obsidian tones (`#0A0A0B` outer, `#09090B` mat, `#121214` inspector/cards, `#0E0E10` rail, `rgba(255,255,255,0.08)` hairlines, `#FAFAFA` primary ink, `#2E2E33` / `#E4E4E7` monochrome switches).

## 2. Typography & Spacing Discipline
- System UI stack: `-apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif`
- Tabular figures (`font-variant-numeric: tabular-nums`) on all L0 blur/exposure/duplicate metrics, page counts, progress figures, and KPI cards.
- Minimum comfortable label size `12px`; `13.5px–14px` body prose.

## 3. Structural Rules (Anti-Clutter Covenant)
1. **No box-around-box clutter**: Right-side toggle rows and L0 telemetry figures sit cleanly on whitespace separated only by `1px` hairlines—never trapped in rounded rectangles.
2. **Quiet topbar & mobile HUD**: Topbar keeps only brand mark, book switcher pill, and borderless inline status/action controls. Mobile `/mobile` puts full-bleed camera viewfinder first under frosted-glass overlays.
3. **Single focal action per view**: Only the primary shutter (`#18181B` pill / double-ring mobile shutter) or primary confirmation (`通过`) commands high contrast; Undo, Upload, and auxiliary actions stay calm and neutral.
4. **Refined top-layer dialogs**: Native `<dialog>` elements animate smoothly with `@starting-style`, `transition-behavior: allow-discrete`, `closedby="any"`, and JS fallback (`closeTopLayer`).
