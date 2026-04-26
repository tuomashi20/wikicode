import sys
from pathlib import Path

path = Path(r"d:\project\wikicode\src\main.py")
content = path.read_bytes()

# Find the start of the mess
start_marker = b'CLI_BANNER = r"""'
end_marker = b'class SlashCommandCompleter'

start_idx = content.find(start_marker)
end_idx = content.find(end_marker)

if start_idx != -1 and end_idx != -1:
    banner_content = r"""
[bold cyan]
      __      __.__ __  .__  _________            .___            
     /  \    /  \__|  | |  | \_   ___ \  ____   __| _/___________ 
     \   \/\/   /  |  | |  | /    \  \/ /  _ \ / __ |/ __ \_  __ \
      \        /|  |  |_|  |_\     \___(  <_> ) /_/ \  ___/|  | \/
       \__/\  / |__|____/____/\______  /\____/\____ |\___  >__|   
            \/                       \/            \/    \/       
[/bold cyan]
"""
    banner_assignment = f'CLI_BANNER = r"""{banner_content}"""\n\n\n'
    new_content = content[:start_idx] + banner_assignment.encode('utf-8') + content[end_idx:]
    path.write_bytes(new_content)
    print("Fixed.")
else:
    print(f"Markers not found: {start_idx}, {end_idx}")
