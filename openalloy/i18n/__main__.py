# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""``python3 -m openalloy.i18n tr [--lang L] MSG [ARG...]``

Prints MSG translated (printf-style ``%s`` arguments are substituted). Used by the
shell installer, which runs before the package is installed. Never fails: on any
problem the English message is printed.
"""

import sys

from openalloy import i18n


def main(argv) -> int:
    if not argv or argv[0] != "tr":
        sys.stderr.write("uso: python3 -m openalloy.i18n tr [--lang L] MSG [ARG...]\n")
        return 2
    args = argv[1:]
    lang = None
    if len(args) >= 2 and args[0] == "--lang":
        lang, args = args[1], args[2:]
    if not args:
        return 0
    msg, rest = args[0], args[1:]
    try:
        i18n.set_language(i18n.resolve(lang))
        text = i18n._(msg)
        if rest:
            text = text % tuple(rest)
    except Exception:
        text = msg
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
