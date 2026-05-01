import base64
cmd = "npx create-next-app@latest . --yes --typescript --tailwind --eslint --app --src-dir --import-alias '@/*'"
encoded = base64.b64encode(cmd.encode('utf-16-le')).decode('ascii')
print(f"powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded}")
