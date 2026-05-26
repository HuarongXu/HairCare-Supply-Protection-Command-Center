import re

with open("dashboards/matres_app.py", "r", encoding="utf-8") as f:
    content = f.read()

# Find the callback section
start = content.index('Output("role-item-table", "columns")')
end = content.index("def update_visuals(")
cb_header = content[start:end]
outputs = re.findall(r"Output\(", cb_header)
print(f"Number of Output() declarations: {len(outputs)}")

# Find return values
func_start = content.index("def update_visuals(")
func_end = content.index("# ── Production Dimension Detail callback", func_start)
func_body = content[func_start:func_end]

ret_start = func_body.rfind("return (")
ret_section = func_body[ret_start:]
ret_end = ret_section.index(")")
ret_block = ret_section[len("return ("):ret_end]

vals = [line.strip().rstrip(",") for line in ret_block.split("\n") if line.strip() and line.strip() != ""]
print(f"Number of return values: {len(vals)}")
for i, v in enumerate(vals, 1):
    print(f"  {i}: {v}")
