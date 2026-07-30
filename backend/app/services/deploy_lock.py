"""Deploy-in-progress marker, shared with scripts/ec2_deploy.sh.

The EC2 deploy replaces the container (`docker rm -f` + `docker run`), which kills any
in-flight run. Crucially the OLD container keeps serving for the entire prune+build
window that precedes the swap, so an operator can start an hour-long run seconds before
it is destroyed with nothing in the UI to warn them. That is exactly how run
a6c3f5d5-a47b-4e38-96c7-b25acf221df0 died: rsync landed at 04:02:15, the run was started
at 04:02:24, and the container was replaced at 04:06:05.

ec2_deploy.sh creates this marker in the host's bind-mounted data directory BEFORE it
starts building and removes it on any exit, so the running backend can refuse to start
new work while a deploy is pending.
"""
import time
from pathlib import Path

from app.config.settings import get_settings
from app.utils.logging import get_logger

logger = get_logger("deploy_lock")

LOCK_FILENAME = ".deploy-in-progress"

# A marker older than this is treated as abandoned. Without this a deploy script killed
# with SIGKILL (so its EXIT trap never ran) would leave the platform permanently unable
# to start a run — a far worse failure than the one this guard exists to prevent.
STALE_AFTER_SECONDS = 45 * 60


def deploy_lock_path() -> Path | None:
    """Resolve the marker path.

    An explicit DEPLOY_LOCK_PATH wins. Otherwise the marker sits next to the SQLite
    database file, because that directory IS the host bind-mount (`-v ./data:/app/data`)
    the deploy script writes to — so prod works with no .env change. Returns None for a
    non-SQLite backend with no explicit path, which disables the guard.
    """
    settings = get_settings()
    if settings.deploy_lock_path:
        return Path(settings.deploy_lock_path)

    url = settings.database_url
    if not url.startswith("sqlite") or "///" not in url:
        return None
    raw = url.split("///", 1)[1].split("?", 1)[0]
    if not raw or raw.startswith(":"):  # e.g. :memory:
        return None
    return Path(raw).expanduser().resolve().parent / LOCK_FILENAME


def is_deploying() -> bool:
    """True while a deploy is staging on this host. Never raises: a lock-check problem
    must not take the API down, so any error degrades to "not deploying"."""
    try:
        path = deploy_lock_path()
        if path is None or not path.exists():
            return False
        age = time.time() - path.stat().st_mtime
        if age > STALE_AFTER_SECONDS:
            logger.warning(
                "Ignoring stale deploy lock at %s (age %.0fs > %ds)",
                path, age, STALE_AFTER_SECONDS,
            )
            return False
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("Deploy lock check failed (treating as not deploying): %s", e)
        return False
