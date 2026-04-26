import sys
from pathlib import Path

path = Path(r"d:\project\wikicode\src\main.py")
content = path.read_bytes()

# The target block to replace (Build mode)
# Start with 'elif session_mode == "build":'
# End with 'plain_chat_turn = False'

# Let's find the specific block.
# We know it starts around 3254 in the current (possibly messed up) file.
# But we need a robust search.

search_pattern = b'elif session_mode == "build":'
# Since there might be two, we want to find the one that is followed by the OLD style code.
# The old style has 'console.print("[bold cyan]>>>'

# New style code:
new_build_code = r"""                elif session_mode == "build":
                    from rich.panel import Panel
                    from rich.syntax import Syntax
                    from rich.box import ROUNDED
                    from rich.live import Live
                    from rich.spinner import Spinner
                    
                    console.print(f"\n[bold blue]You:[/bold blue] {cmd}")
                    agent_build = BuildAgent(config)
                    state = {"auto_all": False}

                    def _cli_on_step(step: BuildStep) -> bool:
                        # 1. 思考卡片
                        console.print(Panel(step.thought, title="[bold yellow]Agent Thought[/bold yellow]", border_style="yellow", box=ROUNDED))
                        
                        if step.action_type == "finish":
                            return True
                            
                        # 2. 拟执行代码卡片
                        syntax_lang = "python" if step.action_type == "python" else "shell"
                        code_syntax = Syntax(step.action_input, syntax_lang, theme="monokai", line_numbers=True)
                        console.print(Panel(code_syntax, title=f"[bold magenta]Proposed Action: {step.action_type}[/bold magenta]", border_style="magenta", box=ROUNDED))
                        
                        # 3. 授权检查
                        if state["auto_all"]:
                            console.print(f"[dim]Auto-executing {step.action_type}...[/dim]")
                        else:
                            ans = console.input("[bold green]Approve execution? (y/a/n): [/bold green]").strip().lower()
                            if ans == 'a':
                                state["auto_all"] = True
                            elif ans == 'n':
                                return False
                        return True

                    try:
                        # 劫持 BuildAgent._execute 以便实时展示结果
                        original_execute = agent_build._execute
                        def _cli_execute_wrapper(action_type, action_input):
                            # 使用 Live Panel 展示正在执行状态
                            with Live(Spinner("dots", text=f"Executing {action_type}..."), console=console, transient=True):
                                res = original_execute(action_type, action_input)
                            
                            # 结果面板
                            console.print(Panel(res, title="[bold green]Execution Output[/bold green]", border_style="green", dim_border=True, box=ROUNDED))
                            return res
                        
                        agent_build._execute = _cli_execute_wrapper
                        final_output = agent_build.run(cmd, history=session_history, on_step=_cli_on_step)
                        resp = AgentResponse(
                            thought="build-mode:complete",
                            actions=["build:done"],
                            output=final_output
                        )
                    except Exception as e:
                        console.print(f"[red]Execution Exception: {e}[/red]")
                        resp = AgentResponse(thought="build:error", actions=[], output=str(e))
                    
                    remember_turn = True
                    plain_chat_turn = False
""".encode('utf-8')

# We'll just replace the first occurrence of the old block if possible.
# Or better, just find the block by its content.

old_block_start = b'elif session_mode == "build":\n                    console.print("[bold cyan]>>>'
# Find the end of this block which is 'plain_chat_turn = False'
end_marker = b'plain_chat_turn = False'

start_idx = content.find(old_block_start)
if start_idx != -1:
    end_idx = content.find(end_marker, start_idx)
    if end_idx != -1:
        # Include the end marker line
        end_idx = content.find(b'\n', end_idx) + 1
        new_content = content[:start_idx] + new_build_code + content[end_idx:]
        path.write_bytes(new_content)
        print("Build block updated.")
    else:
        print("End marker not found.")
else:
    print("Old build block start not found.")
