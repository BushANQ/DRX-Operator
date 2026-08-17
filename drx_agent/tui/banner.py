"""Startup ASCII banner (DRX-Operator) shown as the first chat message."""

from rich.text import Text

_BANNER = [
    r""" ____  ____  __  __      ___                       _""",
    r"""|  _ \|  _ \ \ \/ /     / _ \ _ __   ___ _ __ __ _| |_ ___  _ __""",
    r"""| | | | |_) | \  /_____| | | | '_ \ / _ \ '__/ _` | __/ _ \| '__|""",
    r"""| |_| |  _ <  /  \_____| |_| | |_) |  __/ | | (_| | || (_) | |""",
    r"""|____/|_| \_\/_/\_\     \___/| .__/ \___|_|  \__,_|\__\___/|_|""",
    r"""                             |_|""",
]
_GRADIENT = ["#58a6ff", "#4fb8fe", "#3fc5c8", "#3fb950", "#56d364", "#7ee787"]
_AUTHOR = "by BushSEC · github.com/BushANQ"


def build_banner() -> Text:
    out = Text()
    for line, color in zip(_BANNER, _GRADIENT):
        out.append(line.rstrip() + "\n", style=f"bold {color}")
    width = max(len(r) for r in _BANNER)
    out.append(" " * max(0, width - len(_AUTHOR) + 1))
    out.append(_AUTHOR + "\n", style="#8b949e")
    return out
