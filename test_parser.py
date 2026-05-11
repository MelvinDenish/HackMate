"""Test file parser directly."""
from utils.file_parser import parse_file_blocks

# Build test string carefully - backtick triple
TICK = chr(96) * 3

out = f"""{TICK}file:app.js
console.log('hi');
{TICK}

{TICK}file:index.html
<h1>Hi</h1>
{TICK}"""

print("Input lines:")
for i, l in enumerate(out.split("\n")):
    print(f"  {i}: [{l}]")

parsed = parse_file_blocks(out)
print(f"\nParsed: {len(parsed)} files")
for p in parsed:
    print(f"  path={p[0]}, content_len={len(p[1])}")

assert len(parsed) == 2, f"Expected 2, got {len(parsed)}: {[p[0] for p in parsed]}"
print("\nPASS: 2 files parsed correctly")
