"""把 scripts/*.sh 规范化为「UTF-8 无 BOM + LF 行尾」。

为什么需要这个脚本：本项目的开发环境是 Windows，而 shell 脚本一旦带上
CRLF 行尾，在 Linux 上会以 `bash: $'\\r': command not found` 之类的
神秘错误失败；带 BOM 则会让 shebang 失效（`#!/usr/bin/env bash` 变成
`\ufeff#!/usr/bin/env bash`，内核找不到这个解释器）。

两者都是**静默的**：在 Windows 上用编辑器打开完全看不出问题。
"""

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

UTF8_BOM = b"\xef\xbb\xbf"


def main() -> int:
    changed: list[str] = []
    for path in sorted(SCRIPTS.glob("*.sh")):
        raw = path.read_bytes()

        had_bom = raw.startswith(UTF8_BOM)
        body = raw[len(UTF8_BOM):] if had_bom else raw

        had_crlf = b"\r\n" in body
        fixed = body.replace(b"\r\n", b"\n").replace(b"\r", b"\n")

        if had_bom or had_crlf:
            path.write_bytes(fixed)
            flags = []
            if had_bom:
                flags.append("去掉 BOM")
            if had_crlf:
                flags.append("CRLF->LF")
            changed.append(f"{path.name}: {', '.join(flags)}")
        else:
            changed.append(f"{path.name}: 已合规")

    for line in changed:
        print(line)

    if any("已合规" not in line for line in changed):
        print("\n已修正，请重新运行测试确认。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
