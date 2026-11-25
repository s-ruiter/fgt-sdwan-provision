"""Self-update: compare the running commit with GitHub main of the (public) repo, and update by running ct/install.sh
(the same script as a manual update; it keeps the port) through systemd-run, so it survives the service restart."""
import subprocess
import time

import requests

REPO = "s-ruiter/fgt-sdwan-provision"
BRANCH = "main"
CACHE_S = 600
CACHE = {}


def current(app_dir):
    """Full commit of the running checkout, or None (not a git checkout, e.g. a dev run)."""
    try:
        r = subprocess.run(["git", "-C", app_dir, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    sha = r.stdout.strip()
    return sha if r.returncode == 0 and len(sha) == 40 else None


def check(app_dir, fetch=requests.get):
    """{current, latest, behind, commits: [{sha, title}], error}; cached CACHE_S seconds."""
    now = time.time()
    if CACHE.get("t", 0) > now - CACHE_S and CACHE.get("dir") == app_dir:
        return CACHE["v"]
    own = current(app_dir)
    out = {"current": own[:7] if own else None, "latest": None, "behind": 0, "commits": [], "error": None}
    if not own:
        out["error"] = "version unknown (not a git checkout): updates are not available"
    else:
        try:
            r = fetch(f"https://api.github.com/repos/{REPO}/compare/{own}...{BRANCH}", timeout=8,
                      headers={"Accept": "application/vnd.github+json"})
            if r.status_code != 200:
                raise OSError(f"GitHub answered {r.status_code}")
            j = r.json()
            commits = [{"sha": c["sha"][:7], "title": (c.get("commit", {}).get("message") or "").split("\n", 1)[0]}
                       for c in j.get("commits") or []]
            out.update(behind=int(j.get("ahead_by") or 0), commits=commits,
                       latest=commits[-1]["sha"] if commits else own[:7])
        except Exception as e:                                       # no internet, rate limit, unknown commit ...
            out["error"] = f"could not check for updates ({type(e).__name__}: {str(e)[:120]})"
    CACHE.update(t=now, dir=app_dir, v=out)
    return out


UNIT = "fgt-sdwan-update"


def start(app_dir, run=subprocess.run, status_file=None):
    """Start ct/install.sh as its own systemd unit (it restarts this service, so it must not be a child of it). With a
    status_file the exit code of install.sh is written there (a failed update does not restart the service)."""
    cmd = ["systemd-run", "--unit", UNIT, "--collect", "bash"]
    if status_file:
        cmd += ["-c", f'bash "{app_dir}/ct/install.sh"; echo $? > "{status_file}"']
    else:
        cmd += [f"{app_dir}/ct/install.sh"]
    run(cmd, check=True, capture_output=True, text=True, timeout=20)


def active(run=subprocess.run):
    """True while the update unit runs (deployments and cleanups must wait: the update restarts the service)."""
    try:
        return run(["systemctl", "is-active", "--quiet", UNIT], timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def last_result(status_file):
    """{"rc": exit code of the last update, "at": when} or None."""
    import os
    try:
        with open(status_file) as f:
            return {"rc": int(f.read().strip() or "1"), "at": os.path.getmtime(status_file)}
    except (OSError, ValueError):
        return None
