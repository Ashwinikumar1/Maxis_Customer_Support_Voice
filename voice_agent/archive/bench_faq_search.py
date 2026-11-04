"""Latency check for the FAQ search tool.

  cd voice_agent && uv run python scripts/bench_faq_search.py
"""

import statistics
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

from voice_assistant.faq_search_tool import _search  # noqa: E402

QUERIES = [
    "how do I activate a roaming pass",
    "5G coverage in Ipoh",
    "how to check my bill in the Maxis app",
    "what is Hotlink prepaid",
    "Maxis fibre plans",
]

times = []
for i, q in enumerate(QUERIES * 3):
  t = time.perf_counter()
  results = _search(q)
  ms = (time.perf_counter() - t) * 1000
  if i == 0:
    print(f"cold: {ms:.0f} ms")
  else:
    times.append(ms)
  print(f"{ms:6.0f} ms  {len(results)} results  {q}")
  if i < len(QUERIES):
    for s in results:
      print(f"           - {s}")

print(f"\nwarm p50 {statistics.median(times):.0f} ms, max {max(times):.0f} ms")
