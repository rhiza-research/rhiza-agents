# Phase 11: Lumino Panel Layout

## Goal

Replace the ad-hoc CSS flexbox layout and manual panel toggle logic with Lumino (the layout framework from JupyterLab). Users get a VS Code-like experience: dockable, resizable, draggable panels with persistent layout state. Adding new panels becomes trivial — create a widget, dock it.

## Prerequisites

Phase 10 (extended thinking) complete. All existing panels (sidebar, chat, files, activity) functional.

## Problem

The current frontend has several layout issues:

1. **Inconsistent panel behavior.** Each panel (sidebar, files, activity) has its own show/hide mechanism — different toggle buttons, close buttons, CSS classes, and localStorage keys. Adding a new panel means wiring up all of this from scratch.

2. **No resizing.** Panel widths are fixed in CSS. The sidebar is always 260px. The files and activity panels can't be resized. Users can't allocate more space to what matters for their current task.

3. **No rearrangement.** Panels are locked in position. Files is always right-of-chat, activity is always rightmost. Users can't put activity below chat, or tab files and activity together, or move the conversation list to the right.

4. **Fragile layout code.** `chat.js` is 900 lines of interleaved DOM manipulation, SSE streaming, panel toggle logic, file viewer state, and activity rendering. There's no separation between layout concerns and feature logic.

## Design

### Lumino Overview

[Lumino](https://github.com/jupyterlab/lumino) is the layout engine behind JupyterLab. It provides:

- **DockPanel** — a container that supports split/tabbed/dockable child widgets
- **Widget** — base class with lifecycle hooks (`onAfterAttach`, `onCloseRequest`, etc.)
- **Layout serialization** — `saveLayout()` / `restoreLayout()` for persisting user arrangements
- **Built-in drag-and-drop** — users can drag tabs to rearrange panels

Lumino is a client-side library with no server dependency. It can be loaded from CDN as ES modules, matching the existing pattern (`marked`, `highlight.js`).

### Widget Architecture

Each UI panel becomes a Lumino `Widget` subclass. The widget owns its DOM node and internal logic. The `DockPanel` manages positioning, sizing, and drag/drop.

| Widget | Content | Default Position |
|--------|---------|-----------------|
| `ConversationListWidget` | Sidebar: new chat button, conversation list, user info/logout | Left dock |
| `ChatWidget` | Message area, input form, "Review code" toggle | Center (always visible) |
| `FilesWidget` | File list + file viewer (code display, download, approve) | Right dock |
| `ActivityWidget` | Thinking, tool calls, tool results | Right dock (tabbed with Files) |

### Loading Lumino

Use CDN imports matching the existing pattern:

```javascript
import { DockPanel, Widget, BoxPanel } from 'https://esm.sh/@lumino/widgets@2';
import { MessageLoop } from 'https://esm.sh/@lumino/messaging@2';
```

CSS:
```html
<link rel="stylesheet" href="https://esm.sh/@lumino/default-theme@2/style/index.css">
```

### Layout Persistence

```javascript
// Save on every layout change
dockPanel.layoutModified.connect(() => {
    const config = dockPanel.saveLayout();
    localStorage.setItem('panelLayout', JSON.stringify(config));
});

// Restore on startup
const saved = localStorage.getItem('panelLayout');
if (saved) {
    dockPanel.restoreLayout(JSON.parse(saved));
}
```

Lumino's `saveLayout()` captures split ratios, tab groupings, and panel positions. Widget content state (scroll position, selected file, etc.) is NOT included — widgets manage their own state separately, as they do today.

### Widget Lifecycle

Each widget subclass follows this pattern:

```javascript
class ActivityWidget extends Widget {
    constructor() {
        super();
        this.id = 'activity';
        this.title.label = 'Activity';
        this.title.closable = true;
        // Build internal DOM in this.node
    }

    onAfterAttach(msg) {
        // Widget is now in the DOM — safe to set up event listeners
    }

    onCloseRequest(msg) {
        // User closed the tab — hide but don't destroy
        this.hide();
    }
}
```

### Module Structure

Split `chat.js` (900 lines) into focused modules:

```
static/
    app.js              # Entry point: create DockPanel, instantiate widgets, restore layout
    widgets/
        chat.js         # ChatWidget: messages, input, SSE streaming
        conversations.js # ConversationListWidget: sidebar list, new chat
        files.js        # FilesWidget: file list, viewer, download, approve
        activity.js     # ActivityWidget: thinking, tool calls, results
    lib/
        markdown.js     # marked.js + highlight.js setup (shared by chat + activity)
        api.js          # Fetch helpers for /api/* endpoints
```

Each widget module exports a single class. `app.js` imports them all, creates instances, and docks them into the layout.

### Template Changes

`chat.html` simplifies dramatically — just a shell for Lumino to mount into:

```html
<body>
    <div id="app"></div>
    <link rel="stylesheet" href="https://esm.sh/@lumino/default-theme@2/style/index.css">
    <link rel="stylesheet" href="/static/style.css">
    <script type="module" src="/static/app.js"></script>
</body>
```

Lumino creates and manages all DOM structure. The template passes server data (conversations, current conversation ID, user name) via a `<script type="application/json">` block or data attributes.

### CSS Theming

Lumino provides a default theme (`@lumino/default-theme`). We override it with our existing dark theme colors:

```css
/* Override Lumino's default theme to match our dark UI */
.lm-DockPanel { background: #1a1a2e; }
.lm-TabBar { background: #16162a; }
.lm-TabBar-tab { color: #ccc; }
.lm-TabBar-tab.lm-mod-current { color: white; border-bottom-color: #4a90d9; }
```

The existing `style.css` rules for message bubbles, code blocks, form inputs, etc. remain unchanged — they apply to DOM inside widgets, not to the layout shell.

### Server-Side Changes

Minimal. The Jinja template simplifies but the API endpoints don't change. The server still renders `chat.html` with conversation list and current conversation data. The only difference is that the template no longer contains panel HTML — it's just a mount point.

The `static_version` cache buster should be updated to use a content hash or build timestamp for the new module files.

## Implementation Plan

### Step 1: Module Split

Split `chat.js` into the module structure above WITHOUT changing any behavior. Each module exports the same functions that `chat.js` currently has. `app.js` imports and calls them. Verify everything still works identically.

This is the riskiest step — test thoroughly before proceeding.

### Step 2: Widget Wrappers

Wrap each module's DOM in a Lumino `Widget` subclass. At this point the widgets are just containers — Lumino manages their position/size, but the internal behavior is unchanged.

- Add Lumino CDN imports
- Create widget classes that build their DOM in the constructor
- Create a `DockPanel`, add widgets to it
- Mount the dock panel into `#app`
- Simplify `chat.html` to just the mount point

### Step 3: Layout Features

- Enable drag-and-drop tab rearrangement
- Add layout persistence via `saveLayout()` / `restoreLayout()`
- Add "Reset Layout" option (in config page or as a menu item)
- Wire up `title.closable` so panels can be hidden via tab close
- Add a way to re-show closed panels (menu, keyboard shortcut, or button)

### Step 4: Polish

- Theme Lumino components to match the dark UI
- Handle edge cases: very narrow viewports, all panels closed, etc.
- Remove dead CSS from the old layout (sidebar toggle, panel toggle buttons, etc.)
- Test with existing conversations, streaming, file viewer, HITL approval

## What Doesn't Change

- **All API endpoints** — same SSE streaming, same REST endpoints
- **Message rendering** — same marked.js + highlight.js pipeline
- **SSE event handling** — same streaming logic, just inside `ChatWidget`
- **File viewer** — same code display + download + approve logic, inside `FilesWidget`
- **Activity panel** — same thinking/tool_call/tool_result rendering, inside `ActivityWidget`
- **Server-side rendering** — Jinja still renders the page shell with conversation data

## Risks

1. **CDN module resolution.** Lumino has internal cross-package imports (`@lumino/widgets` imports `@lumino/messaging`, etc.). ESM CDNs like esm.sh handle this automatically, but worth validating early.

2. **Widget focus/activation.** Lumino has an activation system (which widget is "active"). SSE streaming, keyboard shortcuts, and form focus need to work correctly when panels are rearranged.

3. **Module split regressions.** Step 1 (splitting chat.js) touches every feature. Careful testing of streaming, HITL, file viewer, activity panel, new chat, conversation switching.
