"""Background worker: `python -m shots.worker run-once` runs one idempotent update cycle and exits.

Task Scheduler starts it at boot/logon and every 3 hours (deploy/install.ps1). See cycle.py (lock,
decision, bookkeeping), due.py (skip-if-fresh and due-work rules), lock.py (overlap lock).
"""
