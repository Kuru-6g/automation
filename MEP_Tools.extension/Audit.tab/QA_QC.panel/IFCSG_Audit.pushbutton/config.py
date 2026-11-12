# -*- coding: utf-8 -*-
"""Shift+Click: open the IFC-SG audit checklist (audit_config.json) for editing."""
from __future__ import print_function

import os

from pyrevit import forms

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit_config.json")


def open_with_shell(path):
    try:
        os.startfile(path)
        return True
    except Exception:
        pass
    try:
        from System.Diagnostics import Process, ProcessStartInfo

        info = ProcessStartInfo(path)
        info.UseShellExecute = True  # required on .NET 8 (Revit 2025+)
        Process.Start(info)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    if not os.path.isfile(CONFIG_FILE):
        forms.alert(
            "audit_config.json was not found next to the script.",
            sub_msg=CONFIG_FILE,
            warn_icon=True,
        )
    elif not open_with_shell(CONFIG_FILE):
        forms.alert("Could not open the config file. Edit it manually:", sub_msg=CONFIG_FILE)
