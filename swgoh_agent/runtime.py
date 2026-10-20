"""One input-producing agent per workspace, shared by CLI and dashboard."""
from pathlib import Path
import fcntl


class AgentLease:
    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open('a+')
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            self.handle = None
            raise RuntimeError('Agent is already running. Stop it before starting or editing rules.') from None
        return self

    def __exit__(self, *exc):
        if self.handle:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None

    @classmethod
    def busy(cls, path: Path) -> bool:
        try:
            with cls(path):
                return False
        except RuntimeError:
            return True
