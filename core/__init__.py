"""
What every package may import, itself importing none of them: the settings
(config.py, from the environment and the .env file, which envfile.py reads
and writes), the errors a caller can act on (errors.py), periodic work
(background.py), importing a package's files (discovery.py) and child
processes (procs.py).

Nothing is imported here, so importing one of them loads only what it needs
(config.py reads the .env file when it is imported).
"""

from pathlib import Path

# The folder the service runs from: where the .env file and the default data folders are, and the child processes'
# working directory and PYTHONPATH.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
