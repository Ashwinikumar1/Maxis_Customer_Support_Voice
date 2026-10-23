"""Creates or updates voice_agent/.env from .env.example, without touching values you set.

  uv run python scripts/setup_env.py              # add missing settings (backs up .env first)
  uv run python scripts/setup_env.py --check      # only report what's missing; change nothing
  uv run python scripts/setup_env.py --set ROUTING_MODE=specialist --set WRITE_TOOLS_ENABLED=false
  uv run python scripts/setup_env.py --rebuild    # rewrite .env in .env.example's layout, with every
                                                  # explanation, keeping all your values

What it does:
- No .env yet: copies .env.example to .env.
- .env exists: appends every setting that is in .env.example but not in .env, with the
  example's value and comment, under a dated "added by setup_env.py" header. Existing
  lines, values and comments are kept exactly as they are.
- --set KEY=VALUE: sets that value (replacing an existing line for KEY, or appending it).
- --rebuild: writes .env from .env.example's lines and comments, with YOUR value for every key you
  have. An optional "# KEY=..." line you have set becomes "KEY=<your value>". Keys you have that
  the example doesn't know go at the end, under their own header. Nothing you set is lost.
- Before writing, the current .env is copied to .env.bak.<timestamp>.
- Values are never printed for keys that look secret (KEY, SECRET, TOKEN, PASSWORD, URL with
  credentials), only their names.

Standard library only, so it runs before `uv sync` too (python3 scripts/setup_env.py).
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
EXAMPLE = APP / ".env.example"
ENV = APP / ".env"
_KEY_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
_SECRET = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|DB_URL)", re.I)


def parse(lines: list[str]) -> dict[str, int]:
  """KEY -> index of the line that sets it (last one wins, like python-dotenv)."""
  keys = {}
  for i, line in enumerate(lines):
    m = _KEY_LINE.match(line)
    if m:
      keys[m.group(1)] = i
  return keys


def example_blocks(lines: list[str]) -> dict[str, list[str]]:
  """KEY -> its line in .env.example plus the comment lines directly above it."""
  blocks, comments = {}, []
  for line in lines:
    stripped = line.strip()
    if stripped.startswith("#") and not stripped.startswith("# ---") and not stripped.startswith("# ==="):
      comments.append(line)
      continue
    m = _KEY_LINE.match(line)
    if m:
      blocks[m.group(1)] = comments + [line]
    comments = []
  return blocks


def _backup() -> Path:
  """Copies .env to .env.bak.<time> (never overwriting an earlier backup) and returns the path."""
  stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
  backup, n = ENV.with_name(f".env.bak.{stamp}"), 1
  while backup.exists():  # two runs in the same second keep both backups
    backup, n = ENV.with_name(f".env.bak.{stamp}-{n}"), n + 1
  shutil.copyfile(ENV, backup)
  return backup


_OPTIONAL = re.compile(r"^#\s?([A-Z][A-Z0-9_]*)=(.*)$")


def rebuild(example: list[str], current: list[str]) -> list[str]:
  """.env.example's lines and comments, filled with the values from `current`."""
  have = parse(current)
  value = {k: _KEY_LINE.match(current[i]).group(2) for k, i in have.items()}
  out, used = [], set()
  for line in example:
    m = _KEY_LINE.match(line)
    opt = _OPTIONAL.match(line.strip()) if not m else None
    if m and m.group(1) in value:
      out.append(f"{m.group(1)}={value[m.group(1)]}")
      used.add(m.group(1))
    elif opt and opt.group(1) in value and opt.group(1) not in used:
      out.append(f"{opt.group(1)}={value[opt.group(1)]}")  # an optional setting you use
      used.add(opt.group(1))
    else:
      out.append(line)
  extra = [k for k in have if k not in used]
  if extra:
    out += ["", "# -----------------------------------------------------------------------------",
            "# Your other settings (not in .env.example; kept as they were)",
            "# -----------------------------------------------------------------------------"]
    out += [f"{k}={value[k]}" for k in extra]
  return out


def main_set_only(updates) -> int:
  current = ENV.read_text(encoding="utf-8").splitlines()
  for key, val in updates:
    idx = parse(current).get(key)
    if idx is None:
      current.append(f"{key}={val}")
    else:
      current[idx] = f"{key}={val}"
    print("  set     " + shown(key, val))
  ENV.write_text("\n".join(current) + "\n", encoding="utf-8")
  return 0


def shown(key: str, value: str) -> str:
  value = value.split("#", 1)[0].strip()
  return f"{key}=<hidden>" if _SECRET.search(key) and value else f"{key}={value}"


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  parser.add_argument("--check", action="store_true", help="report missing settings only; change nothing")
  parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="set a value")
  parser.add_argument("--rebuild", action="store_true",
                      help="rewrite .env in .env.example's layout and comments, keeping your values")
  args = parser.parse_args()

  if not EXAMPLE.exists():
    print(f"Missing {EXAMPLE}", file=sys.stderr)
    return 1
  example = EXAMPLE.read_text(encoding="utf-8").splitlines()

  if not ENV.exists():
    if args.check:
      print(".env does not exist yet: run without --check to create it from .env.example.")
      return 1
    shutil.copyfile(EXAMPLE, ENV)
    print(f"Created {ENV.relative_to(APP)} from .env.example. Review GOOGLE_CLOUD_PROJECT, "
          "FAQ_SEARCH_ENGINE_ID and GCS_BUCKET for your project.")
    current = ENV.read_text(encoding="utf-8").splitlines()
  else:
    current = ENV.read_text(encoding="utf-8").splitlines()

  have = parse(current)
  blocks = example_blocks(example)
  missing = [k for k in blocks if k not in have]
  updates = []
  for item in args.set:
    if "=" not in item:
      print(f"--set needs KEY=VALUE, got {item!r}", file=sys.stderr)
      return 2
    key, value = item.split("=", 1)
    updates.append((key.strip(), value))

  if args.check:
    if missing:
      print(f"{len(missing)} setting(s) in .env.example are missing from .env (defaults apply):")
      for k in missing:
        print("  " + shown(k, _KEY_LINE.match(blocks[k][-1]).group(2)))
    else:
      print(".env has every setting in .env.example.")
    return 0

  if args.rebuild:
    out = rebuild(example, current)
    backup = _backup()
    ENV.write_text("\n".join(out) + "\n", encoding="utf-8")
    kept = [k for k in parse(current) if k in parse(out)]
    print(f"Rebuilt .env in .env.example's layout (backup: {backup.name}): kept your values for "
          f"{len(kept)} setting(s), added {len(missing)} missing one(s).")
    for k in missing:
      print("  added   " + shown(k, _KEY_LINE.match(blocks[k][-1]).group(2)))
    if updates:
      return main_set_only(updates)
    return 0

  if not missing and not updates:
    print(".env is up to date: every setting in .env.example is present. Nothing changed.")
    return 0

  backup = _backup()

  out = list(current)
  if missing:
    out += ["", f"# --- Added by scripts/setup_env.py on {datetime.now():%Y-%m-%d} from .env.example"]
    for k in missing:
      out += [""] + blocks[k]
  for key, value in updates:
    line = f"{key}={value}"
    idx = parse(out).get(key)
    if idx is None:
      out.append(line)
    else:
      out[idx] = line
  ENV.write_text("\n".join(out) + "\n", encoding="utf-8")

  print(f"Updated .env (backup: {backup.name})")
  for k in missing:
    print("  added   " + shown(k, _KEY_LINE.match(blocks[k][-1]).group(2)))
  for key, value in updates:
    print("  set     " + shown(key, value))
  return 0


if __name__ == "__main__":
  sys.exit(main())
