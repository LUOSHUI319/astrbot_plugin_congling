"""UTF-8 / common GBK-mojibake guard; read-only, including pages and docs."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_EXTENSIONS = {".py", ".md", ".json", ".js", ".mjs", ".css", ".html",
                   ".yaml", ".yml", ".txt", ".toml", ".ini", ".cfg", ".sql"}
# Escapes keep the scanner's own source from being a positive sample.
SUSPECT = re.compile(
    "\u7481\u677f\u7e42|\u93c3\u5815\u68ff|\u6d93\u5d86\u69f8|"
    "\u9422\u3126\u57db|\u7487\u8bf2\u5f47|\u6fb6\u8fab\u89e6|"
    "\u951b|\u9286|\u9983|\u951f\u65a4\u62f7|\ufffd|[\ue000-\uf8ff]"
)


def scan(root: Path = ROOT) -> dict:
    files = 0
    hits, examples = [], []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        if any(part in {".git", "__pycache__", ".venv", "node_modules"} for part in path.parts):
            continue
        files += 1
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeError as exc:
            hits.append({"file": rel, "line": 0, "error": type(exc).__name__})
            continue
        for number, line in enumerate(text.splitlines(), 1):
            matches = SUSPECT.findall(line)
            if not matches:
                continue
            record = {"file": rel, "line": number, "matches": matches}
            # The construction plan quotes one intentionally bad example.
            if (rel == "docs/最终更新总计划_20261101.md"
                    and "这类乱码" in line and matches == ["\u7481\u677f\u7e42"]):
                examples.append(record)
            else:
                hits.append(record)
    return {"files": files, "hits": hits, "documented_examples": examples}


if __name__ == "__main__":
    import json
    result = scan()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(bool(result["hits"]))
