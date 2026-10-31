# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Everton Vilhena Cardoso
"""Messages printed by Python's argparse (usage, errors, built-in help).

argparse translates them with ``gettext``; ``openalloy.i18n.install_argparse()``
routes those calls to OpenAlloy's catalog. This function is never called: it only
lists the user-visible argparse messages (Python 3.10-3.14) so ``tools/i18n.py``
extracts them. Messages that only signal programming errors are left out.
"""

from openalloy.i18n import _, ngettext


def _catalog_only() -> None:  # pragma: no cover - extraction only
    _("usage: ")
    _("positional arguments")
    _("options")
    _("subcommands")
    _("%(heading)s:")
    _(" (default: %(default)s)")
    _("show this help message and exit")
    _("show program's version number and exit")
    _("%(prog)s: error: %(message)s\n")
    _("%(prog)s: warning: %(message)s\n")
    _("the following arguments are required: %s")
    _("one of the arguments %s is required")
    _("unrecognized arguments: %s")
    _("unexpected option string: %s")
    _("ambiguous option: %(option)s could match %(matches)s")
    _("argument %(argument_name)s: %(message)s")
    _("argument '%(argument_name)s' is deprecated")
    _("option '%(option)s' is deprecated")
    _("command '%(parser_name)s' is deprecated")
    _("invalid choice: %(value)r (choose from %(choices)s)")
    _("invalid %(type)s value: %(value)r")
    _("not allowed with argument %s")
    _("ignored explicit argument %r")
    _("expected one argument")
    _("expected at least one argument")
    _("expected at most one argument")
    ngettext("expected %s argument", "expected %s arguments", 1)
    _("unknown parser %(parser_name)r (choices: %(choices)s)")
    _("can't open '%(filename)s': %(error)s")
    _('argument "-" with mode %r')
