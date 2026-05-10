import { Plugin, PluginSettingTab, Setting, ItemView, MarkdownRenderer, requestUrl, WorkspaceLeaf, TFile, Notice, FileSystemAdapter } from 'obsidian';

// --- 常量声明 ---
const VIEW_TYPE_WIKICODIAN = "wikicodian-chat-view";

interface WikicodianSettings {
    serverUrl: string;
    agentSearchLimit: number;
    boostTerms: string;
    reportTemplate: string;
}

const DEFAULT_SETTINGS: WikicodianSettings = {
    serverUrl: 'http://127.0.0.1:8000',
    agentSearchLimit: 8,
    boostTerms: "结算, 标准, 纪要, 2024, 66号",
    reportTemplate: ""
};

let SLASH_COMMANDS: any[] = [
    { name: "/help", desc: "📡 正在从后端同步指令集..." }
];

// --- 视图实现 ---
class WikicodianView extends ItemView {
    settings: WikicodianSettings;
    plugin: WikicodianPlugin;
    messages: any[] = [];
    suggestIndex: number = -1;
    filteredCommands: any[] = [];
    chatMode: string = "chat";
    messageContainer: HTMLElement;
    inputEl: HTMLTextAreaElement;
    inputHistory: string[] = [];
    historyIndex: number = -1;
    suggestContainer: HTMLElement;
    taskContainer: HTMLElement;
    fileTagEl: HTMLElement;
    wikiStatusEl: HTMLElement;
    currentActiveFile: TFile | null = null;
    updateActiveFileUI: () => void;
    allSeenTasks: string[] = [];
    completedTasks: Set<string> = new Set();
    modeSelect: HTMLSelectElement;

    constructor(leaf: WorkspaceLeaf, plugin: WikicodianPlugin) {
        super(leaf);
        this.plugin = plugin;
        this.settings = plugin.settings;
    }

    getViewType() { return VIEW_TYPE_WIKICODIAN; }
    getDisplayText() { return "Wikicodian Chat"; }
    getIcon() { return "bot"; }

    async onOpen() {
        const container = this.containerEl.children[1] as HTMLElement;
        container.empty();
        container.addClass('wikicodian-chat-container');

        // [动态命令加载]
        try {
            const cmdRes = await requestUrl({ url: `${this.settings.serverUrl}/v1/commands` });
            if (cmdRes.json && Array.isArray(cmdRes.json)) {
                SLASH_COMMANDS = cmdRes.json;
            }
        } catch (e) {
            console.error("Failed to load commands from backend:", e);
        }

        // 状态栏
        const statusBar = container.createEl('div', { cls: 'wikicodian-sync-bar' });
        const statusInfo = statusBar.createEl('div');
        statusInfo.createEl('span', { cls: 'wikicodian-status-dot wikicodian-status-online' });
        statusInfo.createSpan({ text: 'WikiCoder Online' });

        const syncBtn = statusBar.createEl('button', { text: '同步专家图谱', cls: 'mod-cta' });
        syncBtn.onclick = () => this.executeSlashCommand("/sync");

        // 固定的任务清单区域
        this.taskContainer = container.createEl('div', { cls: 'wikicodian-task-panel' });
        (this.taskContainer as any).style.display = 'none';

        // 消息区域
        this.messageContainer = container.createEl('div', { cls: 'wikicodian-chat-messages markdown-preview-view' });

        // 命令建议
        this.suggestContainer = container.createEl('div', { cls: 'wikicodian-suggest-container' });
        (this.suggestContainer as any).style.display = 'none';

        // 输入区域外部包装器
        const inputWrapper = container.createEl('div', { cls: 'wikicodian-input-wrapper' });

        this.inputEl = inputWrapper.createEl('textarea', { 
            cls: 'wikicodian-chat-input',
            attr: { placeholder: 'What\'s new, 老板?' }
        }) as HTMLTextAreaElement;

        // 底部控制栏
        const controlBar = inputWrapper.createEl('div', { cls: 'wikicodian-control-bar' });

        // 左侧控制区
        const controlLeft = controlBar.createEl('div', { cls: 'wikicodian-control-left' });

        // 模式选择
        this.modeSelect = controlLeft.createEl('select', { cls: 'wikicodian-mode-select' });
        const modes = [{ id: 'chat', label: '💬 Chat' }, { id: 'agent', label: '🤖 Agent' }];
        modes.forEach(m => {
            const opt = this.modeSelect.createEl('option', { value: m.id, text: m.label });
            if (this.chatMode === m.id) opt.selected = true;
        });
        this.modeSelect.onchange = (e) => { this.chatMode = (e.target as HTMLSelectElement).value; };


        // 附件文件标签
        this.fileTagEl = controlLeft.createEl('div', { cls: 'wikicodian-file-tag' });
        this.updateActiveFileUI = () => {
            const activeFile = this.app.workspace.getActiveFile();
            if (activeFile) {
                this.currentActiveFile = activeFile;
                this.fileTagEl.empty();
                this.fileTagEl.createEl('span', { text: '📄', cls: 'wikicodian-file-icon' });
                this.fileTagEl.createEl('span', { text: activeFile.basename });
                const closeBtn = this.fileTagEl.createEl('span', { text: '✕', cls: 'wikicodian-file-close' });
                closeBtn.onclick = (e) => { e.stopPropagation(); this.currentActiveFile = null; this.fileTagEl.style.display = 'none'; };
                this.fileTagEl.style.display = 'flex';
            } else {
                this.currentActiveFile = null;
                this.fileTagEl.style.display = 'none';
            }
        };
        this.updateActiveFileUI();
        this.registerEvent(this.app.workspace.on('file-open', this.updateActiveFileUI));

        // 右侧控制区
        const controlRight = controlBar.createEl('div', { cls: 'wikicodian-control-right' });
        const interruptBtn = controlRight.createEl('button', { text: '⏹ 中断', cls: 'wikicodian-yolo-btn' });
        interruptBtn.style.display = 'none';
        
        let currentAbortController: AbortController | null = null;
        
        interruptBtn.onclick = () => {
            if (currentAbortController) {
                currentAbortController.abort();
                currentAbortController = null;
                new Notice("⏹ 任务已强行中止 (协作式)");
            }
        };

        const handleSend = async () => {
            let query = this.inputEl.value.trim();
            if (!query) return;


            if (query.startsWith("/")) {
                this.executeSlashCommand(query);
                this.inputEl.value = '';
                return;
            }

            // 保存输入历史
            if (!this.inputHistory.includes(query)) {
                this.inputHistory.push(query);
            }
            this.historyIndex = this.inputHistory.length;

            this.inputEl.value = '';
            const attachedPath = (this.currentActiveFile && this.app.vault.adapter instanceof FileSystemAdapter) 
                                 ? this.app.vault.adapter.getFullPath(this.currentActiveFile.path) : "";
            
            let displayQuery = query;
            let backendQuery = query;
            if (attachedPath) {
                displayQuery = `📎 **附件**: \`${this.currentActiveFile?.basename}\`\n${query}`;
                backendQuery = `[附言：当前我在屏幕上正在浏览和编辑该文件：${attachedPath}]\n\n${query}`;
            }

            await this.appendMessage('user', displayQuery);
            this.messages.push({ q: backendQuery, a: "" });
            const currentMsgIndex = this.messages.length - 1;
            
            const statusEl = this.messageContainer.createEl('div', { cls: 'wikicodian-message wikicodian-status-msg' });
            statusEl.setText(`🧠 正在深度拆解指令...`);

            interruptBtn.style.display = 'block';
            currentAbortController = new AbortController();

            try {
                const vaultPath = (this.app.vault.adapter as any).basePath || "";
                // [动态参数映射]：传递 limit 和 boost_terms
                const response = await fetch(`${this.settings.serverUrl}/v1/chat`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ 
                        query: backendQuery, 
                        mode: this.chatMode, 
                        history: this.messages.slice(-5),
                        cwd: vaultPath,
                        agent_search_limit: this.settings.agentSearchLimit,
                        rag_filename_boost_terms: this.settings.boostTerms.split(",").map(t => t.trim()).filter(t => t),
                        report_template: this.settings.reportTemplate
                    }),
                    signal: currentAbortController.signal
                });

                const reader = response.body?.getReader();
                const decoder = new TextDecoder();
                let hasStarted = false;
                let botMsgEl: HTMLElement | null = null;
                let botStatusEl: HTMLElement | null = null;
                let botContentEl: HTMLElement | null = null;
                let fullContent = "";
                const thoughts: string[] = [];
                let isInterrupted = false;
                let interactionData: any = null;

                if (reader) {
                    while (true) {
                        const { done, value } = await reader.read();
                        if (done) break;
                        const chunk = decoder.decode(value);
                        const lines = chunk.split('\n');
                        for (const line of lines) {
                            if (line.startsWith('data: ')) {
                                const dataStr = line.slice(6).trim();
                                if (dataStr === '[DONE]') continue;
                                try {
                                    const json = JSON.parse(dataStr);
                                    if (!hasStarted) {
                                        hasStarted = true;
                                        statusEl.remove();
                                        botMsgEl = await this.appendMessage('bot', '');
                                        botStatusEl = botMsgEl.createEl('div', { cls: 'wikicodian-thought-bubble' });
                                        botContentEl = botMsgEl.createEl('div', { cls: 'markdown-rendered' });
                                    }
                                    if (json.status) {
                                        const isTimer = json.status.includes("(已耗时");
                                        const lastThought = thoughts.length > 0 ? thoughts[thoughts.length - 1] : null;
                                        const lastIsTimer = lastThought && lastThought.includes("(已耗时");

                                        if (isTimer && lastIsTimer) {
                                            // 如果前后都是计时器，直接替换最后一条，实现原地跳秒
                                            thoughts[thoughts.length - 1] = json.status;
                                        } else if (lastThought !== json.status) {
                                            // 否则正常追加（去重）
                                            thoughts.push(json.status);
                                        }

                                        // 动态渲染步骤列表
                                        botStatusEl!.empty();
                                        thoughts.forEach((t, i) => {
                                            botStatusEl!.createEl('div', { 
                                                text: `${i + 1}. ${t}`, 
                                                cls: 'wikicodian-thought-step' 
                                            });
                                        });
                                    }
                                    // [Sync] 捕获交互数据
                                    if (json.interaction && Object.keys(json.interaction).length > 0) {
                                        interactionData = json.interaction;
                                    }
                                    if (json.is_interrupted) {
                                        isInterrupted = true;
                                    }
                                    if (json.output) {
                                        if (botStatusEl && botStatusEl.style.display !== 'none') {
                                            botStatusEl.style.display = 'none';
                                        }
                                        fullContent += json.output;
                                        this.messages[currentMsgIndex].a = fullContent;
                                        
                                        // 立即显示文字（纯文本模式，极速响应）
                                        if (botContentEl) {
                                            botContentEl.empty();
                                            // 使用 MarkdownRenderer 渲染，但加上防抖逻辑（或简单的异步处理）
                                            MarkdownRenderer.renderMarkdown(fullContent, botContentEl, '', this as any);
                                        }
                                        
                                        this.messageContainer.scrollTo(0, this.messageContainer.scrollHeight);
                                    }
                                    
                                    // [Sync] 渲染交互式卡片
                                    if (isInterrupted && interactionData && botContentEl) {
                                        if (!botContentEl.querySelector('.wikicodian-interaction-card')) {
                                            const card = botContentEl.createEl('div', { cls: 'wikicodian-interaction-card' });
                                            card.createEl('div', { text: `❓ ${interactionData.question}`, cls: 'wikicodian-interaction-question' });
                                            const btnContainer = card.createEl('div', { cls: 'wikicodian-interaction-buttons' });
                                            
                                            const options = interactionData.options || ["确认继续", "终止任务"];
                                            options.forEach((opt: string) => {
                                                const btn = btnContainer.createEl('button', { text: opt, cls: 'mod-cta' });
                                                btn.onclick = () => {
                                                    this.inputEl.value = opt;
                                                    card.remove();
                                                    handleSend();
                                                };
                                            });
                                        }
                                    }
                                } catch(e) {}
                            }
                        }
                    }
                }
            } catch (e: any) {
                if (statusEl) statusEl.remove();
                if (e.name === 'AbortError') await this.appendMessage('bot', `⚠️ **任务已强制中止。** 后端已安全回收资源。`);
                else await this.appendMessage('bot', `### ❌ 连接失败\n请检查后端 \`serve\`。`);
            } finally {
                interruptBtn.style.display = 'none';
                currentAbortController = null;
            }
        };

        this.inputEl.oninput = () => {
            const val = this.inputEl.value;
            if (val.startsWith("/")) {
                const search = val.slice(1).toLowerCase();
                this.filteredCommands = SLASH_COMMANDS.filter(c => c.name.toLowerCase().includes(search) || (c.desc && c.desc.toLowerCase().includes(search)));
                if (this.filteredCommands.length > 0) {
                    this.suggestIndex = 0; // 核心：每次输入变化都重置到第一项
                    this.renderSuggest();
                } else {
                    this.suggestContainer.style.display = 'none';
                }
            } else {
                this.suggestContainer.style.display = 'none';
            }
        };

        this.inputEl.onkeydown = (e) => {
            if (this.suggestContainer.style.display !== 'none' && this.filteredCommands.length > 0) {
                if (e.key === 'ArrowDown') {
                    e.preventDefault();
                    this.suggestIndex = (this.suggestIndex + 1) % this.filteredCommands.length;
                    this.renderSuggest();
                } else if (e.key === 'ArrowUp') {
                    e.preventDefault();
                    this.suggestIndex = (this.suggestIndex - 1 + this.filteredCommands.length) % this.filteredCommands.length;
                    this.renderSuggest();
                } else if (e.key === 'Enter' || e.key === 'Tab') {
                    e.preventDefault();
                    const cmd = this.filteredCommands[this.suggestIndex].name;
                    this.inputEl.value = cmd + " ";
                    this.suggestContainer.style.display = 'none';
                    this.inputEl.focus();
                } else if (e.key === 'Escape') {
                    this.suggestContainer.style.display = 'none';
                }
            } else {
                if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    handleSend();
                } else if (e.key === 'ArrowUp') {
                    // 历史记录回溯
                    if (this.historyIndex > 0) {
                        e.preventDefault();
                        this.historyIndex--;
                        this.inputEl.value = this.inputHistory[this.historyIndex];
                    }
                } else if (e.key === 'ArrowDown') {
                    // 历史记录向后
                    if (this.historyIndex < this.inputHistory.length - 1) {
                        e.preventDefault();
                        this.historyIndex++;
                        this.inputEl.value = this.inputHistory[this.historyIndex];
                    } else if (this.historyIndex === this.inputHistory.length - 1) {
                        e.preventDefault();
                        this.historyIndex++;
                        this.inputEl.value = "";
                    }
                }
            }
        };
    }

    renderSuggest() {
        this.suggestContainer.empty();
        this.suggestContainer.style.display = 'block';
        this.filteredCommands.forEach((cmd, i) => {
            const item = this.suggestContainer.createEl('div', { cls: 'wikicodian-suggest-item' });
            if (i === this.suggestIndex) {
                item.addClass('is-selected');
                // 自动滚动到选中项
                setTimeout(() => item.scrollIntoView({ block: 'nearest', behavior: 'smooth' }), 50);
            }
            item.createSpan({ text: cmd.name, cls: 'wikicodian-suggest-name' });
            item.createSpan({ text: cmd.desc, cls: 'wikicodian-suggest-desc' });
            item.onclick = () => {
                this.inputEl.value = cmd.name + " ";
                this.suggestContainer.style.display = 'none';
                this.inputEl.focus();
            };
        });
    }


    async appendMessage(role: string, text: string) {
        const msgEl = this.messageContainer.createEl('div', { cls: `wikicodian-message wikicodian-message-${role}` });
        if (role === 'user') {
            msgEl.setText(text);
        } else {
            msgEl.addClass('markdown-rendered');
            await MarkdownRenderer.renderMarkdown(text, msgEl, '', this as any);
        }
        this.messageContainer.scrollTo(0, this.messageContainer.scrollHeight);
        return msgEl;
    }

    async executeSlashCommand(cmd: string) {
        await this.appendMessage('user', cmd);
        try {
            const res = await requestUrl({
                url: `${this.settings.serverUrl}/v1/exec`,
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ 
                    command: cmd,
                    history: this.messages.slice(-10)
                })
            });
            
            const data = res.json;
            await this.appendMessage('bot', data.output || "执行成功");

            // [逻辑对齐]：处理会话恢复
            if (data.history && Array.isArray(data.history)) {
                this.messages = data.history.map((h: any) => ({ q: h[0], a: h[1] }));
                this.messageContainer.empty();
                for (const h of data.history) {
                    await this.appendMessage('user', h[0]);
                    await this.appendMessage('bot', h[1]);
                }
            }

            // [逻辑对齐]：处理模式同步
            if (data.mode) {
                this.chatMode = data.mode;
                this.modeSelect.value = data.mode;
            }

            // [逻辑对齐]：处理会话重置
            if (cmd.startsWith("/reset")) {
                this.messages = [];
                this.messageContainer.empty();
                await this.appendMessage('bot', "🧹 会话已重置。");
            }
        } catch (e) { 
            await this.appendMessage('bot', "### ❌ 执行失败\n请检查后端连接或指令语法。"); 
        }
    }
}

export default class WikicodianPlugin extends Plugin {
    settings: WikicodianSettings;
    async onload() {
        await this.loadSettings();
        this.registerView(VIEW_TYPE_WIKICODIAN, (leaf) => new WikicodianView(leaf, this));
        this.addRibbonIcon('bot', 'Wikicodian Chat', () => this.activateView());
        this.addSettingTab(new WikicodianSettingTab(this.app, this));
    }
    async activateView() {
        const { workspace } = this.app;
        let leaf = workspace.getLeavesOfType(VIEW_TYPE_WIKICODIAN)[0];
        if (!leaf) {
            const rightLeaf = workspace.getRightLeaf(false);
            if (rightLeaf) {
                leaf = rightLeaf;
                await leaf.setViewState({ type: VIEW_TYPE_WIKICODIAN, active: true });
            }
        }
        if (leaf) workspace.revealLeaf(leaf);
    }
    async loadSettings() { this.settings = Object.assign({}, DEFAULT_SETTINGS, await this.loadData()); }
    async saveSettings() { await this.saveData(this.settings); }
}

class WikicodianSettingTab extends PluginSettingTab {
    plugin: WikicodianPlugin;
    constructor(app: any, plugin: WikicodianPlugin) { super(app, plugin); this.plugin = plugin; }
    display() {
        const { containerEl } = this;
        containerEl.empty();
        containerEl.createEl('h2', { text: 'WikiCoder 插件设置' });

        new Setting(containerEl).setName('Backend URL').addText(text => text.setValue(this.plugin.settings.serverUrl).onChange(async (v) => {
            this.plugin.settings.serverUrl = v; await this.plugin.saveSettings();
        }));

        new Setting(containerEl).setName('检索资料条数').setDesc('WikiAgent 每次对话参考的资料深度 (默认 8)')
            .addSlider(slider => slider.setLimits(1, 20, 1).setValue(this.plugin.settings.agentSearchLimit).onChange(async (v) => {
                this.plugin.settings.agentSearchLimit = v; await this.plugin.saveSettings();
            }));

        new Setting(containerEl).setName('强制加权词库').setDesc('以逗号分隔，命中这些词的文件将优先展示')
            .addText(text => text.setValue(this.plugin.settings.boostTerms).onChange(async (v) => {
                this.plugin.settings.boostTerms = v; await this.plugin.saveSettings();
            }));

        const templateSetting = new Setting(containerEl)
            .setName('报告合成模板')
            .setDesc('指定 AI 生成最终报告时使用的模板 (自动从后端同步)');

        templateSetting.addDropdown(async (dropdown) => {
            // 1. 先添加一个“同步中”的占位符
            dropdown.addOption(this.plugin.settings.reportTemplate, '🔄 正在同步模板...');
            
            try {
                // 2. 向后端请求真实清单
                const res = await requestUrl({ 
                    url: `${this.plugin.settings.serverUrl}/v1/templates`,
                    method: 'GET'
                });
                
                if (res.json && res.json.templates) {
                    // 3. 清空并填充真实选项
                    // 注意：Obsidian 的 dropdown 没有清空方法，我们直接操作其 selectEl
                    const selectEl = (dropdown as any).selectEl as HTMLSelectElement;
                    selectEl.empty();
                    
                    res.json.templates.forEach((t: any) => {
                        dropdown.addOption(t.id, t.name);
                    });

                    // 4. 如果当前没选过，则使用后端返回的默认值
                    if (!this.plugin.settings.reportTemplate || this.plugin.settings.reportTemplate === "business_audit.md") {
                        this.plugin.settings.reportTemplate = res.json.default;
                    }
                    
                    dropdown.setValue(this.plugin.settings.reportTemplate);
                }
            } catch (e) {
                console.error("Failed to sync templates:", e);
                // 失败时回退到默认
                const selectEl = (dropdown as any).selectEl as HTMLSelectElement;
                selectEl.empty();
                dropdown.addOption('business_audit.md', 'Business Audit (Fallback)');
                dropdown.setValue('business_audit.md');
            }

            dropdown.onChange(async (v) => {
                this.plugin.settings.reportTemplate = v;
                await this.plugin.saveSettings();
                new Notice(`📋 已切换模板为: ${v}`);
            });
        });
    }
}
