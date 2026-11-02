"""Settings for the test run."""

import sys

# The tests import frozen code from experiments/harness/; no bytecode is written next to it.
sys.dont_write_bytecode = True
