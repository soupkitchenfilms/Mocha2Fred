"""Mocha Pro startup hook for the Fred exporters.

Point MOCHA_INIT_SCRIPT at this directory (or this file) before launching Mocha Pro.
Headless mochaexport.py does not run this hook. Batch exports
import fred_mocha_exporters and call register() themselves.
"""
import os
import sys

try:
    _HERE = os.path.dirname(os.path.abspath(__file__))
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)

    import fred_mocha_exporters
    fred_mocha_exporters.register()
except Exception as exc:   # never stop Mocha starting over an exporter
    print("Fred exporters not registered: %s" % exc)
