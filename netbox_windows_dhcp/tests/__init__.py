"""Keep the plugin's log lines out of the test output.

Real sync jobs forward their WARNING+ lines to the root logger (and so to the console).
Tests that trigger warnings on purpose would print them between the dots. Tests that
check a log line use `assertLogs`, which doesn't depend on this.
"""

from .. import background_tasks

background_tasks._WarnOnlyPropagator.emit = lambda self, record: None
background_tasks.logger.propagate = False
