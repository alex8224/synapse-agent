import base64
import glob
import os
import re
import sys

# 找到最新的 large_tool_results 文件
files = glob.glob('large_tool_results/call_*')
if not files:
    print("No screenshot files found")
    sys.exit(1)

latest = max(files, key=os.path.getctime)
print(f"Processing: {latest}")

content = open(latest).read()
m = re.search(r"data='([^']+)'", content)
if not m:
    print("No base64 data found")
    sys.exit(1)

os.makedirs('.tmp', exist_ok=True)
ts = str(os.path.getmtime(latest))
out = f'.tmp/screenshot_{ts}.png'
open(out, 'wb').write(base64.b64decode(m.group(1)))
print(f"OK -> {out}")
