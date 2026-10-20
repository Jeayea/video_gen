"""密钥卫生：**工作树里不许出现真实形态的 key**。

review 提醒的第一条风险就是「`.env` 里有真实 OpenRouter key」。
`.env` 本身在 `.gitignore` 里（当前也确实没进过 git），但这类东西一旦被
`git add -A` 或者拷进分享包就会漏。这条用例是一道**便宜的护栏**：
只扫 git 跟踪的文件，发现 `sk-or-v1-` + 长十六进制体就失败。

（历史里出现过的都是测试用的 DUMMY 占位串，形态短、不是十六进制，所以不会误报。）
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

#: 真实 key 的形态：前缀 + 32 位以上十六进制
REAL_KEY = re.compile(r"sk-or-v1-[0-9a-fA-F]{32,}")
#: 其它常见密钥前缀（同样按“前缀 + 长随机串”判定）
OTHER_KEYS = re.compile(r"(?i)\b(sk-[a-z0-9]{32,}|ghp_[A-Za-z0-9]{30,})\b")
#: 更多常见形态：Anthropic / AWS AK / Google API key / Slack token / 私钥块（大小写敏感）
MORE_KEYS = re.compile(
    r"sk-ant-[A-Za-z0-9_-]{20,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bAIza[0-9A-Za-z_-]{35}\b"
    r"|\bxox[abposr]-[0-9A-Za-z-]{10,}"
    r"|-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
)

ROOT = Path(__file__).resolve().parents[1]


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    if out.returncode != 0:  # 不是 git 仓库（例如打包后的源码）就跳过
        return []
    return [ROOT / line for line in out.stdout.splitlines() if line.strip()]


def test_no_real_looking_keys_in_tracked_files():
    offenders: list[str] = []
    for path in _tracked_files():
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:  # pragma: no cover
            continue
        for pattern in (REAL_KEY, OTHER_KEYS, MORE_KEYS):
            for match in pattern.finditer(text):
                offenders.append(f"{path.relative_to(ROOT)}: {match.group(0)[:12]}…")
    assert not offenders, "工作树里出现了像真 key 的字符串：\n" + "\n".join(offenders)


def test_env_is_gitignored():
    """.env 必须始终被忽略（这条用例防的是有人把 .gitignore 里的规则删掉）。"""
    out = subprocess.run(
        ["git", "check-ignore", "-v", ".env"], cwd=ROOT, capture_output=True, text=True
    )
    if out.returncode not in (0, 1):  # 不是 git 仓库
        return
    assert out.returncode == 0, ".env 没有被 .gitignore 忽略"
    listed = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"], cwd=ROOT, capture_output=True
    )
    assert listed.returncode != 0, ".env 被跟踪了，必须移除"
