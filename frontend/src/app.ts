import { CommandRegistry } from '@lumino/commands';
import { BoxPanel, DockLayout, DockPanel, Menu, MenuBar, Widget } from '@lumino/widgets';
import '@lumino/default-theme/style/index.css';
import 'font-awesome/css/font-awesome.css';
import './theme.css';
import './style.css';

import { ConversationListWidget, ConversationData } from './widgets/conversations';
import { ChatWidget } from './widgets/chat';
import { FilesWidget } from './widgets/files';
import { FileViewerWidget } from './widgets/fileviewer';
import { ActivityWidget } from './widgets/activity';
import { ConfigWidget } from './widgets/config';

interface AppData {
    conversations: ConversationData[];
    conversationId: string;
    userName: string;
}

// Read server-provided data
const appDataEl = document.getElementById('app-data');
const appData: AppData = appDataEl ? JSON.parse(appDataEl.textContent || '{}') : {};

// --- Command Registry ---
const commands = new CommandRegistry();

// --- Create Widgets ---
const activityWidget = new ActivityWidget();

const filesWidget: FilesWidget = new FilesWidget({
    getConversationId: () => chatWidget.conversationId,
    getReviewMode: () => chatWidget.reviewMode,
    onRunFile: (path: string) => chatWidget.sendMessage(`Run the file ${path}`),
    onOpenFile: (path: string, content: string, source: string) => {
        const existingId = 'file-' + path.replace(/[^a-zA-Z0-9]/g, '_');
        for (const w of dock.widgets()) {
            if (w.id === existingId) {
                dock.activateWidget(w);
                return;
            }
        }
        const viewer = new FileViewerWidget(path, content, source);
        dock.addWidget(viewer, { mode: 'split-bottom', ref: chatWidget });
        dock.activateWidget(viewer);
    },
});

const chatWidget: ChatWidget = new ChatWidget({
    conversationId: appData.conversationId || '',
    activityWidget,
    filesWidget,
});

const conversationsWidget = new ConversationListWidget({
    conversations: appData.conversations || [],
    currentConversationId: appData.conversationId || '',
    userName: appData.userName || '',
});

const configWidget = new ConfigWidget();


// --- Status Bar ---
class StatusBar extends Widget {
    constructor(userName: string) {
        super();
        this.addClass('status-bar');
        this.node.innerHTML = `
            <span>${userName || 'User'}</span>
            <span style="margin-left:auto"><a href="/logout" class="menu-bar-link">Logout</a></span>
        `;
    }
}

const statusBar = new StatusBar(appData.userName);

// --- Layout: everything in one DockPanel ---
const dock = new DockPanel();
dock.id = 'dock-panel';

// Build initial layout with explicit sizes
const initialLayout: DockLayout.ILayoutConfig = {
    main: {
        type: 'split-area',
        orientation: 'horizontal',
        sizes: [0.15, 0.85],
        children: [
            { type: 'tab-area', widgets: [conversationsWidget], currentIndex: 0 },
            {
                type: 'split-area',
                orientation: 'horizontal',
                sizes: [0.7, 0.3],
                children: [
                    { type: 'tab-area', widgets: [chatWidget, configWidget], currentIndex: 0 },
                    {
                        type: 'split-area',
                        orientation: 'vertical',
                        sizes: [0.6, 0.4],
                        children: [
                            { type: 'tab-area', widgets: [activityWidget], currentIndex: 0 },
                            { type: 'tab-area', widgets: [filesWidget], currentIndex: 0 },
                        ],
                    },
                ],
            },
        ],
    },
};
dock.restoreLayout(initialLayout);
// Config starts hidden — remove it after initial layout
configWidget.parent = null;

// --- Menu Bar ---
const menuBar = new MenuBar();
menuBar.id = 'menu-bar';

// Stack: menu bar + dock + status bar
const mainPanel = new BoxPanel({ direction: 'top-to-bottom' });
mainPanel.id = 'main-panel';
mainPanel.addWidget(menuBar);
mainPanel.addWidget(dock);
mainPanel.addWidget(statusBar);
BoxPanel.setStretch(menuBar, 0);
BoxPanel.setStretch(dock, 1);
BoxPanel.setStretch(statusBar, 0);

// Mount into #app
const appEl = document.getElementById('app')!;
Widget.attach(mainPanel, appEl);

// --- Commands & Menus (AFTER attach so update messages are processed) ---

// View commands: toggle widget visibility, restore to initial layout when re-adding
function addViewCommand(id: string, label: string, widget: Widget) {
    commands.addCommand(id, {
        label,
        isToggled: () => widget.parent !== null,
        execute: () => {
            if (widget.parent) {
                widget.parent = null;
            } else {
                // Re-add by restoring the full initial layout.
                // restoreLayout will place all widgets that are in the config;
                // widgets not in the config get unparented — so we need to
                // save which widgets are currently attached, restore the initial
                // layout, then re-remove the ones that were hidden.
                const wasHidden = new Set<Widget>();
                for (const w of [conversationsWidget, chatWidget, activityWidget, filesWidget, configWidget]) {
                    if (!w.parent && w !== widget) {
                        wasHidden.add(w);
                    }
                }
                dock.restoreLayout(initialLayout);
                for (const w of wasHidden) {
                    w.parent = null;
                }
                dock.activateWidget(widget);
            }
        },
    });
}

addViewCommand('view:chats', 'Chats', conversationsWidget);
addViewCommand('view:activity', 'Activity', activityWidget);
addViewCommand('view:files', 'Files', filesWidget);
addViewCommand('view:config', 'Config', configWidget);

const viewMenu = new Menu({ commands });
viewMenu.title.label = 'View';
viewMenu.addItem({ command: 'view:chats' });
viewMenu.addItem({ command: 'view:activity' });
viewMenu.addItem({ command: 'view:files' });
viewMenu.addItem({ command: 'view:config' });
menuBar.addMenu(viewMenu);

// Resize to fill the window
function resize() {
    mainPanel.fit();
}
window.addEventListener('resize', resize);
resize();
