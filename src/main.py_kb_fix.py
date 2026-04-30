def build_key_bindings() -> KeyBindings:
    """构建 TUI 全局快捷键绑定：彻底解决空格误提交 Bug"""
    from prompt_toolkit.key_binding import KeyBindings
    kb = KeyBindings()

    @kb.add("c-c")
    def _(event):
        """Ctrl+C 立即退出应用"""
        event.app.exit()

    @kb.add("escape")
    def _(event):
        """按下 ESC 取消补全菜单"""
        buf = event.app.current_buffer
        if buf.complete_state:
            buf.cancel_completion()

    @kb.add("enter")
    def _(event):
        """回车键：始终执行提交动作。如果有选中补全项则先填入。"""
        buf = event.app.current_buffer
        if buf.complete_state and buf.complete_state.current_completion:
            buf.apply_completion(buf.complete_state.current_completion)
        buf.validate_and_handle()

    @kb.add(" ")
    def _(event):
        """空格键：仅在有补全项选中时填入，平时仅输入普通空格，绝不触发提交。"""
        buf = event.app.current_buffer
        if buf.complete_state and buf.complete_state.current_completion:
            # 填入选中的补全建议
            buf.apply_completion(buf.complete_state.current_completion)
            # 填入后追加一个空格，方便后续参数输入
            if not buf.text.endswith(" "):
                buf.insert_text(" ")
            # 尝试开启下一级补全显示
            buf.start_completion(select_first=False)
        else:
            # 正常打字状态：仅插入空格字符
            buf.insert_text(" ")
    
    return kb
