# STRATHEX Design System -- Missoula Pro-Am Manager

**Version:** 1.0.0
**Last updated:** 2026-03-23
**Applies to:** Missoula Pro-Am Manager V2.5.0+
**Source of truth:** `static/css/theme.css`

This document is the formal design system specification for the Missoula Pro-Am Manager, the first pilot application in the STRATHEX ecosystem. It codifies every visual decision present in production so that new features, templates, and future STRATHEX apps can be built with full consistency.

---

## Table of Contents

1. [Design Philosophy](#1-design-philosophy)
2. [Color System](#2-color-system)
3. [Typography](#3-typography)
4. [Spacing & Layout](#4-spacing--layout)
5. [Surface Hierarchy](#5-surface-hierarchy)
6. [Component Patterns](#6-component-patterns)
7. [Button System](#7-button-system)
8. [Badge System](#8-badge-system)
9. [Card System](#9-card-system)
10. [Form Controls](#10-form-controls)
11. [Table System](#11-table-system)
12. [Navigation](#12-navigation)
13. [Motion & Transitions](#13-motion--transitions)
14. [Responsive Breakpoints](#14-responsive-breakpoints)
15. [Accessibility](#15-accessibility)
16. [Print Styles](#16-print-styles)
17. [i18n Theme Variants](#17-i18n-theme-variants)
18. [Recommendations for Consistency](#18-recommendations-for-consistency)

---

## 1. Design Philosophy

The STRATHEX design language is a **dark-first, competition-grade UI** built for high-pressure tournament operations. It draws visual metaphors from fire, forged metal, and timber -- the core materials of professional timbersports.

**Core principles:**

- **Dark surfaces**: Reduce eye strain during long show days. Every surface is dark; light mode does not exist.
- **Fire palette accents**: Primary brand color is a high-saturation red (`--sx-fire`), supported by amber and gold. These colors represent energy, urgency, and competition.
- **Information density**: Tournament ops staff need data-rich views. Typography is compact, tables use tabular numerals, and spacing is tight but readable.
- **Bootstrap 5 foundation**: All components build on Bootstrap 5.3.2 with dark-theme overrides. This ensures rapid development while maintaining the STRATHEX identity.
- **Progressive disclosure**: Sidebars collapse, sections fold, modals layer -- complexity is available but not forced.

---

## 2. Color System

### 2.1 Design Tokens (CSS Custom Properties)

All colors are defined as CSS custom properties on `:root` in `theme.css`.

#### Surface Colors

| Token                | Hex         | Role                                      |
|----------------------|-------------|-------------------------------------------|
| `--sx-base`          | `#0b0d11`   | Page background (deepest)                 |
| `--sx-surface`       | `#13161d`   | Card/panel backgrounds                    |
| `--sx-surface-2`     | `#1b1f2a`   | Elevated surfaces (card headers, inputs)  |
| `--sx-surface-3`     | `#222737`   | Highest elevation (input group addons)    |
| `--sx-border`        | `#252a38`   | Default border color                      |
| `--sx-border-bright` | `#363e52`   | Emphasized borders (dropdowns, dividers)  |

#### Fire Palette (Brand)

| Token                | Value                         | Role                          |
|----------------------|-------------------------------|-------------------------------|
| `--sx-fire`          | `#ef2b16`                     | Primary brand red             |
| `--sx-fire-hover`    | `#d51f10`                     | Fire on hover/press           |
| `--sx-fire-glow`     | `rgba(239, 43, 22, 0.34)`    | Box-shadow glow for fire CTA  |
| `--sx-amber`         | `#ff7a1a`                     | Secondary warm accent         |
| `--sx-gold`          | `#f39f1b`                     | Tertiary warm accent          |
| `--sx-gold-bright`   | `#ffd94a`                     | Highlighted gold (badges)     |

#### Text Colors

| Token          | Hex         | Usage                                  |
|----------------|-------------|----------------------------------------|
| `--sx-text`    | `#ece8e0`   | Primary text (warm off-white)          |
| `--sx-text-2`  | `#8c95aa`   | Secondary text (muted, labels)         |
| `--sx-text-3`  | `#555e72`   | Tertiary text (captions, disabled)     |

#### Status Colors

| Token          | Hex         | Semantic              |
|----------------|-------------|-----------------------|
| `--sx-success` | `#2da85e`   | Positive / completed  |
| `--sx-danger`  | `#e83535`   | Error / destructive   |
| `--sx-info`    | `#3b8ee8`   | Informational / links |
| `--sx-warning` | `#e89012`   | Caution / attention   |

#### Status Color Light Variants (used in badges, alerts, backgrounds)

Each status color has a consistent pattern for translucent overlays:

| Context     | Background alpha | Border alpha | Text (light variant) |
|-------------|-----------------|--------------|----------------------|
| `.bg-*`     | `0.20`          | `0.35`       | Lightened hex        |
| `.badge.*`  | `0.20`          | `0.40`       | Lightened hex        |
| `.alert-*`  | `0.12`          | `0.40`       | Lightened hex        |

Light text variants: success `#5cd48a`, danger `#f06060`, info `#6cb6f5`, warning `#f0b84c`.

#### Legacy Aliases

These map old token names to the current system. **Do not use in new code** -- use `--sx-*` tokens directly.

```css
--proam-green, --proam-green-deep, --proam-wood
--proam-gold, --proam-ink, --proam-paper, --proam-panel, --proam-line
--strathex-ember, --strathex-solar, --strathex-red, --strathex-cyan
```

### 2.2 Utility Classes

| Class              | Effect                                        |
|--------------------|-----------------------------------------------|
| `.text-sx-muted`   | `color: var(--sx-text-2)`                     |
| `.text-sx-subtle`  | `color: var(--sx-text-3)`                     |
| `.text-sx-fire`    | `color: var(--sx-fire)`                       |
| `.text-sx-amber`   | `color: var(--sx-amber)`                      |
| `.text-sx-gold`    | `color: var(--sx-gold)`                       |
| `.bg-sx-surface`   | `background: var(--sx-surface)`               |
| `.bg-sx-surface-2` | `background: var(--sx-surface-2)`             |
| `.bg-sx-surface-3` | `background: var(--sx-surface-3)`             |
| `.border-sx`       | `border-color: var(--sx-border)`              |
| `.border-sx-bright`| `border-color: var(--sx-border-bright)`       |

---

## 3. Typography

### 3.1 Font Stack

| Role        | Family                              | Loaded via               |
|-------------|-------------------------------------|--------------------------|
| **Display** | `"Fraunces", Georgia, serif`        | Google Fonts (500, 700)  |
| **Data**    | `"Orbitron", sans-serif`            | Google Fonts (500, 700, 800) |
| **Body**    | `"Inter", "Segoe UI", sans-serif`   | Google Fonts (400-700)   |

**Usage rules:**

- **Fraunces**: Headings (`h1`-`h6`), empty state titles, sidebar tournament name. Serif warmth offsets the technical dark UI.
- **Orbitron**: Brand wordmark, hero titles, stat values (`.data-value`), status beacons, ownership labels. Geometric/futuristic; use for numeric data and competition identity.
- **Inter**: Body text, buttons, form labels, card headers, sidebar nav links. The workhorse -- everything that is not a heading or data value.

### 3.2 Type Scale

| Class / Context     | Size       | Weight | Extra                                      |
|---------------------|------------|--------|---------------------------------------------|
| `body`              | `0.925rem` | 400    | Line-height default; antialiased            |
| `h1`                | Bootstrap  | 700    | Fraunces; `letter-spacing: 0.01em`          |
| `.hero-title`       | `1.72rem`  | 800    | Orbitron; uppercase; `letter-spacing: 0.04em` |
| `.data-value`       | `1.5rem`   | 700    | Orbitron; `letter-spacing: -0.01em`; tabular |
| `.data-label`       | `0.65rem`  | 600    | Inter; uppercase; `letter-spacing: 0.1em`   |
| `.text-label`       | `0.7rem`   | 700    | Inter; uppercase; `letter-spacing: 0.1em`   |
| `.form-label`       | `0.84rem`  | 500    | Inter                                        |
| `.badge`            | `0.72rem`  | 600    | Inter; `letter-spacing: 0.04em`             |
| `.text-xs`          | `0.72rem`  | --     | Utility                                      |
| `.text-xxs`         | `0.62rem`  | --     | Utility                                      |
| `.sidebar .nav-link`| `0.875rem` | --     | Inter                                        |
| `.navbar .nav-link` | `0.855rem` | 500    | Inter                                        |
| `.dropdown-item`    | `0.875rem` | --     | Inter                                        |
| `.breadcrumb`       | `0.8rem`   | --     | Inter                                        |
| `.alert`            | `0.88rem`  | --     | Inter                                        |
| `.ownership-label`  | `0.55rem`  | 700    | Orbitron; uppercase; `letter-spacing: 0.14em` |
| `.brand-title strong`| `0.82rem` | 800    | Orbitron; uppercase; `letter-spacing: 0.06em` |
| `.brand-title small` | `0.58rem` | --     | Inter; uppercase; `letter-spacing: 0.1em`   |
| `.footer-meta`      | `0.76rem`  | --     | Inter                                        |

### 3.3 Font Utility Classes

| Class            | Effect                                 |
|------------------|----------------------------------------|
| `.text-orbitron` | `font-family: "Orbitron", sans-serif`  |
| `.text-fraunces` | `font-family: "Fraunces", Georgia, serif` |

### 3.4 Numeric Formatting

All `<td>` and `<th>` cells use `font-variant-numeric: tabular-nums` for aligned columns in competition result tables.

---

## 4. Spacing & Layout

### 4.1 General Approach

Spacing follows Bootstrap 5's utility system (`p-*`, `m-*`, `gap-*`). No custom spacing scale is defined beyond Bootstrap defaults. Key patterns:

| Pattern                | Value          | Context                        |
|------------------------|----------------|--------------------------------|
| Page container padding | `py-4`         | `container-fluid` on content   |
| Card body padding      | Bootstrap default + custom | `0.85rem 1.15rem` on headers |
| Card border-radius     | `12px`         | All `.card`                    |
| Modal border-radius    | `14px`         | `.modal-content`               |
| Alert border-radius    | `10px`         | `.alert`                       |
| Dropdown border-radius | `10px`         | `.dropdown-menu`               |
| Dropdown item radius   | `6px`          | `.dropdown-item`               |
| Button border-radius   | Bootstrap default | Inherited from BS5           |
| Sidebar width          | `220px` expanded / `44px` collapsed | `#appSidebar`     |
| Sidebar sticky offset  | `top: 0`       | `height: calc(100vh - 90px)`   |
| Ownership bar height   | `22px` min     | `.ownership-inner`             |

### 4.2 Grid System

- Standard Bootstrap 12-column grid (`container-fluid` preferred over `container`)
- Footer: CSS Grid 3-column layout (`1fr auto 1fr`), collapsing to 1-column at `<768px`
- Sidebar + main: Flexbox (`d-flex`), sidebar fixed-width, main `flex-grow-1`

### 4.3 Z-Index Scale

| Layer                 | z-index    |
|-----------------------|------------|
| Sidebar               | `100`      |
| Mobile sidebar drawer | `1050`     |
| Mobile backdrop       | `1049`     |
| Toast container       | `1100`     |
| Arapaho lock banner   | `2100`     |
| Brainrot overlays     | `2500-2800`|
| Arapaho loading bar   | `9999`     |
| Skip-to-main link     | `99999`    |

---

## 5. Surface Hierarchy

The dark theme uses a 6-level depth system. Each step up adds subtle brightness to communicate elevation:

```
Level 0: --sx-base       #0b0d11  (page background)
Level 1: --sx-surface     #13161d  (cards, panels, sidebar)
Level 2: --sx-surface-2   #1b1f2a  (card headers, form inputs, elevated panels)
Level 3: --sx-surface-3   #222737  (input-group-text, highest cards)
Level 4: --sx-border      #252a38  (borders between surfaces)
Level 5: --sx-border-bright #363e52 (emphasized borders, dividers)
```

**Gradient overlays** add depth on top of flat surfaces:
- Navbar: `linear-gradient(180deg, #0c0c11, #09090d)`
- Sidebar: `linear-gradient(180deg, #131620, #0e1016)`
- Ownership bar: `linear-gradient(180deg, #040507, #07080b)`
- Card top shimmer: `linear-gradient(90deg, transparent, rgba(255,255,255,0.06), transparent)` (1px `::before` pseudo-element)

---

## 6. Component Patterns

### 6.1 Ownership Bar

A slim branding strip at the top of every page. Contains a 1px fire-gradient accent line (`::before`), the "Missoula Pro-Am" label in Orbitron, and "by STRATHEX" right-aligned. During live competition phases, the label is replaced by a `.status-beacon` with a pulsing green dot.

### 6.2 Status Beacon

```html
<span class="status-beacon">
    <span class="status-beacon-dot"></span>
    College Active
</span>
```

Green pill with pulsing dot. Orbitron font, uppercase, 0.6rem. Animation: `beaconPulse` (opacity + scale, 1.6s infinite).

### 6.3 STRATHEX Chip

Navbar brand pill with logo + "by STRATHEX" text. Dark background, fire border, hover lifts and glows.

### 6.4 Empty State

```html
<div class="empty-state">
    <i class="bi bi-inbox empty-state-icon"></i>
    <div class="empty-state-title">No competitors yet</div>
    <div class="empty-state-body">Import or add competitors to get started.</div>
    <a href="..." class="btn btn-proam btn-sm">Add Competitor</a>
</div>
```

Centered, generous padding (3.5rem top/bottom). Title in Fraunces, body capped at `36ch`. Compact variant: `.empty-state-sm`.

### 6.5 Stat Card

```html
<div class="stat-card stat-card-fire">
    <div class="data-value">42</div>
    <div class="data-label">Pro Competitors</div>
</div>
```

Variants: `.stat-card-fire` (red left border + gradient), `.stat-card-gold` (gold left border + gradient).

### 6.6 Phase Banner

```html
<div class="phase-banner phase-banner-pro">
    <i class="bi bi-broadcast"></i>
    Pro Show is Live
</div>
```

Variants: `-college` (blue), `-pro` (amber), `-setup` (grey), `-completed` (green). Translucent background + matching border + colored text.

### 6.7 Position Medallions

| Class           | Gradient                                      | Text   |
|-----------------|-----------------------------------------------|--------|
| `.position-1st` | Gold: `#b8860b -> #ffd700 -> #b8860b`         | Dark   |
| `.position-2nd` | Silver: `#708090 -> #c0c8d0 -> #708090`       | Dark   |
| `.position-3rd` | Bronze: `#8b4513 -> #cd7f32 -> #8b4513`       | White  |

28px circles, 800 weight, inline-flex centered.

### 6.8 Skeleton Loaders

```html
<tr class="skeleton-row">
    <td><span class="skeleton-cell" style="--skeleton-w: 60%"></span></td>
</tr>
```

Sweeping gradient animation (`skeleton-sweep`, 1.5s infinite). Width customizable via `--skeleton-w` variable.

### 6.9 Heat Progress Bar

4px slim progress indicator: `--sx-border` track, green gradient fill. Smooth width transition (0.4s ease).

### 6.10 Sticky Save Bar

Fixed to bottom of scroll context. Dark background, top border, heavy shadow. For form save actions.

---

## 7. Button System

### 7.1 Base

All buttons use `font-weight: 600`, `letter-spacing: 0.01em`, Inter font.

### 7.2 Variants

| Class                   | Background                                          | Border            | Text     | Shadow                        |
|-------------------------|-----------------------------------------------------|-------------------|----------|-------------------------------|
| `.btn-proam`            | `linear-gradient(135deg, #e8391f, #c02010)`         | `#e8391f`         | White    | Fire glow                     |
| `.btn-proam-pulse`      | Same as `.btn-proam` + glow pulse animation         | Same              | White    | Animated fire glow (2.6s)     |
| `.btn-primary`          | `--sx-info` solid                                   | `--sx-info`       | White    | None                          |
| `.btn-success`          | `--sx-success` solid                                | `--sx-success`    | White    | None                          |
| `.btn-warning`          | `--sx-amber` solid                                  | `--sx-amber`      | White    | None                          |
| `.btn-danger`           | `--sx-danger` solid                                 | `--sx-danger`     | White    | None                          |
| `.btn-secondary`        | `--sx-surface-2`                                    | `--sx-border-bright` | `--sx-text-2` | None                  |
| `.btn-outline-primary`  | Transparent -> `rgba(info, 0.15)` on hover          | `rgba(info, 0.5)` | Info blue | None                         |
| `.btn-outline-success`  | Transparent -> `rgba(success, 0.15)` on hover       | `rgba(success, 0.5)` | Success green | None                |
| `.btn-outline-warning`  | Transparent -> `rgba(warning, 0.15)` on hover       | `rgba(amber, 0.5)` | Amber   | None                          |
| `.btn-outline-danger`   | Transparent -> `rgba(danger, 0.15)` on hover        | `rgba(danger, 0.5)` | Danger red | None                   |
| `.btn-outline-secondary`| Transparent -> `--sx-surface-2` on hover            | `--sx-border-bright` | `--sx-text-2` | None               |
| `.btn-outline-light`    | Transparent -> `--sx-surface-2` on hover            | `--sx-border-bright` | `--sx-text-2` | None               |

### 7.3 Interaction States

- **`.btn-proam` hover**: Brighter gradient, stronger glow shadow, `translateY(-1px)` lift
- **`.btn-proam` active**: `translateY(0)`, reduced shadow
- **`.btn-proam-pulse`**: `proam-glow-pulse` keyframes (2.6s ease-in-out infinite); stops on hover
- **`.btn-loading`**: `pointer-events: none; opacity: 0.7` -- for async button states

### 7.4 Usage Guidelines

| Context                    | Recommended Button         |
|----------------------------|----------------------------|
| Primary CTA (1 per page)  | `.btn-proam.btn-proam-pulse` |
| Secondary CTA              | `.btn-proam`               |
| Standard actions           | `.btn-primary`             |
| Confirmations              | `.btn-success`             |
| Destructive actions        | `.btn-danger`              |
| Cancel / dismiss           | `.btn-secondary`           |
| Toolbar / low emphasis     | `.btn-outline-*`           |

---

## 8. Badge System

### 8.1 Bootstrap Override Badges

All Bootstrap badge classes are overridden to use translucent backgrounds with colored borders (glass-morphism style on dark surfaces):

| Class              | Bg alpha | Border alpha | Text color |
|--------------------|----------|--------------|------------|
| `.badge.bg-primary`| `0.20`   | `0.40`       | `#6cb6f5`  |
| `.badge.bg-success`| `0.20`   | `0.40`       | `#5cd48a`  |
| `.badge.bg-warning`| `0.20`   | `0.40`       | `#f0b84c`  |
| `.badge.bg-danger` | `0.20`   | `0.40`       | `#f06060`  |
| `.badge.bg-secondary`| `0.07` | border-bright | `--sx-text-2` |
| `.badge.bg-info`   | `0.20`   | `0.40`       | `#6cb6f5`  |

Base badge style: `font-weight: 600; letter-spacing: 0.04em; font-size: 0.72rem`.

### 8.2 Podium Badges

| Class           | Background                                            | Text     |
|-----------------|-------------------------------------------------------|----------|
| `.badge-gold`   | `linear-gradient(135deg, --sx-gold, --sx-gold-bright)`| `#1a1000`|
| `.badge-silver` | `linear-gradient(135deg, #7a8a98, #b0bcc8)`          | `#111`   |
| `.badge-bronze` | `linear-gradient(135deg, #b06a28, #d48838)`           | `#fff`   |

---

## 9. Card System

### 9.1 Base Card

```css
background: var(--sx-surface);
border: 1px solid var(--sx-border);
border-radius: 12px;
box-shadow: 0 4px 20px rgba(0,0,0,0.35);
```

A subtle 1px top shimmer (`::before` pseudo-element) adds an inset highlight effect. Hover increases shadow depth.

### 9.2 Card Headers

| Class                    | Background                                     | Bottom border                  | Text color  |
|--------------------------|-------------------------------------------------|-------------------------------|-------------|
| `.card-header` (default) | `var(--sx-surface-2)`                           | `1px solid --sx-border`       | `--sx-text` |
| `.card-header-proam`     | `linear-gradient(135deg, #1e0e08, #2a1208)`    | `2px solid --sx-fire`         | `--sx-text` |
| `.card-header-action`    | `linear-gradient(135deg, #0e1f35, #162d4a)`    | `2px solid rgba(info, 0.45)`  | `#6cb6f5`   |
| `.card-header-neutral`   | `var(--sx-surface-2)`                           | `1px solid --sx-border`       | `--sx-text` |
| `.card-header-alert`     | `linear-gradient(135deg, #291800, #3a2208)`     | `2px solid rgba(warning, 0.45)`| `#f0b84c`  |
| `.card-header-gold`      | `linear-gradient(135deg, #1e1200, #2e1c00)`     | `2px solid rgba(gold, 0.55)`  | `--sx-gold-bright` |
| `.card-header.bg-danger` | `linear-gradient(135deg, #2a0a0a, #3d1010)`     | `2px solid rgba(danger, 0.55)`| `#ff7070`   |

**Pattern**: Each semantic header uses a dark gradient matching its color family, with a 2px bottom border at ~50% opacity of the accent color.

### 9.3 Card Footer

```css
background: rgba(0,0,0,0.20);
border-top: 1px solid var(--sx-border);
```

---

## 10. Form Controls

### 10.1 Inputs & Selects

- Background: `--sx-surface-2`
- Border: `--sx-border`
- Text: `--sx-text`
- Placeholder: `--sx-text-3`
- **Focus ring**: Border becomes `--sx-fire`, box-shadow `0 0 0 3px var(--sx-fire-glow)`. This is the signature STRATHEX focus pattern -- fire-red instead of Bootstrap's default blue.

### 10.2 Labels

`color: --sx-text-2; font-size: 0.84rem; font-weight: 500`

### 10.3 Checkboxes / Radios

- Unchecked: `background: --sx-surface-2; border: --sx-border-bright`
- Checked: `background: --sx-fire; border: --sx-fire` -- red check marks, consistent with brand

### 10.4 Input Group Text

`background: --sx-surface-3; border: --sx-border; color: --sx-text-2`

---

## 11. Table System

### 11.1 Base Table

```css
color: var(--sx-text);
border-color: var(--sx-border);
background: transparent;
font-variant-numeric: tabular-nums;
```

Hover rows: `background: rgba(255,255,255,0.03)`.

### 11.2 Branded Table

`.table-proam` adds a fire-themed header:

```css
thead: linear-gradient(135deg, #1e0e08, #2a1208)
th: uppercase, 0.72rem, letter-spacing 0.06em, 2px fire bottom border
```

### 11.3 Sticky Table Headers

Add `.table-sticky` to any `<table>`. For scrollable containers, wrap in `.table-scroll-wrap` (`max-height: 70vh`).

### 11.4 Deactivated Rows

`.table-secondary > *`: `background: rgba(255,255,255,0.03); opacity: 0.65`

---

## 12. Navigation

### 12.1 Top Navbar

```css
.navbar-proam:
  background: linear-gradient(180deg, #0c0c11, #09090d)
  border-bottom: 2px fire-to-amber gradient (border-image)
  box-shadow: 0 2px 20px rgba(0,0,0,0.65), 0 0 60px rgba(fire, 0.08)
```

Nav links: `--sx-text-2`, 0.855rem, 500 weight. Active state: fire-tinted background (`rgba(fire, 0.12)`) with `#f0735a` text.

### 12.2 Sidebar

- Width: 220px expanded, 44px collapsed
- Sticky, `calc(100vh - 90px)` height
- Background: subtle vertical gradient
- Nav links: 8px border-radius, 0.875rem, 0.15s transition
- Active link: Fire-tinted background + 3px left border + `#f0735a` text
- Section groups: Independently collapsible with chevron toggles
- Collapsed state: Icons enlarge to 1.1rem, labels hidden

### 12.3 Mobile Sidebar Drawer

At `<992px`, the sidebar becomes a fixed overlay (260px, z-index 1050) with a blurred backdrop. Triggered by `#mobileNavToggle` button.

### 12.4 Breadcrumbs

Transparent background, 0.8rem. Link color: `--sx-text-2` (hover: `--sx-text`). Active: `--sx-text-3`.

### 12.5 Dropdowns

```css
background: --sx-surface-2;
border: 1px solid --sx-border-bright;
border-radius: 10px;
box-shadow: 0 8px 32px rgba(0,0,0,0.55);
```

Items: 6px radius, 1px margin. Active items use fire-tinted background.

---

## 13. Motion & Transitions

### 13.1 Timing Guidelines

| Duration     | Easing         | Use case                          |
|--------------|----------------|-----------------------------------|
| `0.12s`      | `ease`         | Link color transitions            |
| `0.15s`      | (default)      | Nav link hover, sidebar links     |
| `0.18s`      | `ease-out`     | Page fade-in, skip link           |
| `0.2s`       | `ease`         | Button transitions, sidebar width |
| `0.22s`      | `ease`         | Card hover shadow                 |
| `0.25s`      | `ease`         | Mobile backdrop fade              |
| `0.4s`       | `ease`         | Progress bar fill                 |

### 13.2 Named Animations

| Animation           | Duration | Easing            | Description                               |
|----------------------|---------|-------------------|-------------------------------------------|
| `page-fade-in`      | `0.18s` | `ease-out`        | Main content: opacity 0->1, translateY 7->0 |
| `proam-glow-pulse`  | `2.6s`  | `ease-in-out`     | Primary CTA fire glow breathing           |
| `skeleton-sweep`    | `1.5s`  | `ease-in-out`     | Loading placeholder shimmer               |
| `beaconPulse`       | `1.6s`  | `ease-in-out`     | Status dot opacity + scale pulse          |
| `arp-sweep`         | `0.9s`  | `linear`          | Arapaho flag loading bar                  |

### 13.3 Interaction Motion

- **Button lift**: `.btn-proam:hover` -- `translateY(-1px)` with shadow expansion
- **Quick launch hover**: `translateX(4px)` slide-right
- **STRATHEX chip hover**: `translateY(-1px)` with glow expansion
- **Sidebar collapse**: `transition: width 0.2s ease` on `#appSidebar`
- **Card hover**: Shadow deepens from `0 4px 20px` to `0 10px 36px`

---

## 14. Responsive Breakpoints

The system follows Bootstrap 5 breakpoints with these specific adaptations:

| Breakpoint  | Width         | Key changes                                    |
|-------------|---------------|------------------------------------------------|
| `xs`        | `<576px`      | Compact brand logo (34px), smaller ownership bar, reduced hero sizes |
| `sm`        | `>=576px`     | Default mobile layout                          |
| `md`        | `>=768px`     | Footer switches from 1-col to 3-col grid       |
| `lg`        | `>=992px`     | Desktop sidebar visible, mobile drawer hidden, navbar labels shown |
| `xl`        | `>=1200px`    | Tournament name visible in navbar              |

### 14.1 Critical Breakpoint: 992px

This is the primary layout shift. Below 992px:
- Desktop sidebar hides (`d-none d-lg-flex`)
- Mobile sidebar drawer available via toggle
- STRATHEX chip text label hidden
- Brand subtitle hidden
- Navbar text labels collapse to icon-only

### 14.2 Portal Mobile Mode

`body.portal-mobile` strips: ownership bar, STRATHEX chip, footer. Navbar becomes sticky top. Background darkens to `#0d1015`. Designed for field-side tablet/phone scoring.

---

## 15. Accessibility

### 15.1 Skip Link

`.skip-to-main`: Hidden above viewport (`top: -60px`), revealed on `:focus` (`top: 0`). Fire-red background, white text, 700 weight.

### 15.2 Focus Visible

Global `:focus-visible` ring: `2px solid var(--sx-fire)` with `2px offset`. Mouse clicks suppress the ring on non-form elements. Form controls use the fire-glow box-shadow instead.

### 15.3 Color Contrast

Primary text (`#ece8e0`) on base background (`#0b0d11`): contrast ratio ~14:1 (AAA).
Secondary text (`#8c95aa`) on base: ~5.5:1 (AA).
Tertiary text (`#555e72`) on base: ~3.2:1 -- used only for captions and non-essential decorative text.

### 15.4 ARIA Patterns

- Sidebar: `aria-current="page"` on active nav link
- Toasts: `role="alert" aria-live="assertive"`
- Mobile backdrop: `aria-hidden="true"`
- Loading bar: `aria-hidden="true"`
- Tooltips: Bootstrap `data-bs-toggle="tooltip"` on status badges

---

## 16. Print Styles

`@media print` resets the entire dark theme:

- Background: white; text: black
- All cards: white bg, 1px #999 border, no shadow, `break-inside: avoid`
- Table headers: `#efefef` background
- Badges: grey background, dark text
- Hidden: `.no-print`, buttons, forms, modals, toasts, sidebar, pagination
- Links: black, no underline

---

## 17. i18n Theme Variants

### 17.1 Northern Arapaho Mode (`body.lang-arp`)

Token overrides:
- `--sx-fire: #c40000` (deeper crimson)
- `--sx-amber: #330000` (near-black)
- `--sx-gold / --sx-gold-bright: #ffffff`

Visual changes:
- Flag background image (full viewport, fixed)
- 88% opacity dark overlay
- All fire-colored elements shift to deeper crimson
- STRATHEX chip becomes crimson-tinted
- Activates the Arapaho flag loading bar animation

---

## 18. Recommendations for Consistency

The following observations note areas where the current implementation could be tightened for maximum consistency. **These are not bugs** -- they are opportunities for future refinement.

### 18.1 Token Consolidation

- **Light text variants** (`#5cd48a`, `#6cb6f5`, `#f0b84c`, `#f06060`, `#ff7070`, `#68e898`, `#7dc2ff`, `#ffbf60`) are hardcoded hex values throughout the CSS rather than CSS custom properties. Extracting these as `--sx-success-light`, `--sx-info-light`, etc. would make palette changes a single-edit operation.

### 18.2 Card Header Duplication

- `.card-header.bg-primary` is defined twice (line ~93 and ~1553) with slightly different values. The second definition (Improvement 7) wins via cascade, but the first is dead code. Removing the earlier definition would eliminate confusion.

### 18.3 Border Radius Scale

- Currently: `6px` (dropdown items), `8px` (sidebar links, various), `10px` (alerts, dropdowns, stat cards), `12px` (cards), `14px` (modals, login card), `16px` (hero badges, empty state icons), `999px` (pills/beacons). Consider defining a formal radius scale as tokens: `--sx-radius-sm: 6px`, `--sx-radius-md: 10px`, `--sx-radius-lg: 14px`, `--sx-radius-pill: 999px`.

### 18.4 Shadow Scale

- Shadows use ad-hoc values. A 3-tier shadow token system (`--sx-shadow-sm`, `--sx-shadow-md`, `--sx-shadow-lg`) would standardize elevation.

### 18.5 Transition Timing

- Most transitions use `0.15s` or `0.2s` but there is no shared token. A `--sx-transition-fast: 0.15s` and `--sx-transition-normal: 0.22s` pair would ensure consistency.

### 18.6 Semantic Class Naming

- `.btn-proam` is the app-specific primary CTA. For STRATHEX ecosystem portability, consider aliasing it as `.btn-sx-primary` while keeping `.btn-proam` as an app-level alias.

### 18.7 Missing Dark Overrides

- Bootstrap's `.btn-close` uses `filter: invert(1) grayscale(1) brightness(0.6)` which works but produces a slightly different visual weight than the rest of the system. A custom SVG close icon in `--sx-text-2` would be more precise.

### 18.8 Arapaho Mode Specificity

- The Arapaho/brainrot CSS constitutes approximately 40% of the theme file by line count. Extracting it to a separate `theme-arp.css` loaded conditionally (only when `CURRENT_LANG == 'arp'`) would reduce the default CSS payload significantly.

---

## Appendix: File Reference

| File                         | Purpose                                        |
|------------------------------|------------------------------------------------|
| `static/css/theme.css`       | Single source of truth for all design tokens and component styles |
| `templates/base.html`        | Shell template: navbar, sidebar, footer, toasts, font loading |
| `templates/auth/login.html`  | Login page with dual-logo lockup pattern       |
| `templates/dashboard.html`   | Command Centre hero, stat cards, quick launch  |
| `templates/_sidebar.html`    | Collapsible sidebar with section groups        |

**External dependencies:**
- Bootstrap 5.3.2 (CSS + JS via CDN)
- Bootstrap Icons 1.11.1 (via CDN)
- Google Fonts: Fraunces (500, 700), Orbitron (500, 700, 800), Inter (400-700)
