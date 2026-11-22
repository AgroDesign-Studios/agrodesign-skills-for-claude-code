"""Run a command as a Linux child subreaper, hold its tree to a budget, and kill what it leaves.

usage: python3 -I codex_contain.py <report-file> -- <command> [args...]

Why. A Codex run's sandbox can outlive Codex: when Codex interrupts a hung command it
may kill only the sandbox's outer `bwrap`, and the sandbox's own pid-1 process
(`codex-linux-sandbox`) is then adopted by host pid 1 and keeps running whatever it
ran (a recursion that never ends, for hours). bwrap's --die-with-parent does not help
where an AppArmor profile clears the parent-death signal at exec, and bwrap's
--new-session puts the sandbox out of reach of a process-group or session kill.

How.
- This process sets PR_SET_CHILD_SUBREAPER, so any descendant whose parent dies is
  re-parented to it instead of to pid 1. While the command runs, every such orphan is
  killed together with everything under it, within about a second. When the command
  exits, whatever is still attached is killed the same way.
- A tree is killed by stopping it first: every process in it gets SIGSTOP, the tree is
  walked again until no new process turns up (a stopped process cannot fork), and then
  all of them get SIGKILL. So a tree of any depth goes in one pass.
- The live command's own tree is held to a budget: every poll counts its tasks
  (threads included) and its resident memory, and above TREE_TASKS tasks or
  TREE_MEM_MB MiB the whole tree, the command included, is killed the same way and the
  report says which budget it broke. Polling, not a kernel limit (an account without a
  systemd user manager has no cgroup of its own), so a tree can overshoot by what it
  allocates within one poll.
- Under that, the command runs with RLIMIT_NPROC at this user's current task count plus
  NPROC_HEADROOM, a kernel limit on forks between two polls. It counts every task of
  the user, so other sessions use up headroom too.
- When this process is not in a terminal's foreground process group, it leaves its
  caller's group, so a kill aimed at that group does not take the containment down
  with it. When its parent dies it kills the whole tree at once: nobody is left to
  wait for the result.

Report: one line, written to <report-file> when the command has finished:
    subreaper=on reaped_during=<n> reaped_after=<n> remaining=<n> budget=<ok|tasks|memory> names=<comm,...>
`reaped_during` counts processes killed while the command ran, `reaped_after` those
killed after it ended, `remaining` those still alive after the last kill pass (each
process is counted once). `budget` is `tasks` or `memory` when the command's tree broke
that budget and was killed. With subreaper=off nothing could be contained and the
command was not run. A SIGTERM, SIGINT or SIGHUP to this process is passed on to the
command as SIGTERM, then as SIGKILL 30 s later if it is still running, and what it
leaves is still collected before this exits.

Env: CODEX_CONTAIN_TREE_TASKS, CODEX_CONTAIN_TREE_MEM_MB, CODEX_CONTAIN_NPROC_HEADROOM
override the budgets. Linux only (prctl, /proc).

Exit status: the command's own (128+N when a signal N ended it); 70 when the subreaper
or the task cap could not be set or the command could not be started.
"""

import ctypes
import glob
import os
import re
import resource
import signal
import sys
import time

PR_SET_CHILD_SUBREAPER = 36
POLL_SECONDS = 0.5
AFTER_SECONDS = 30.0
NPROC_HEADROOM = 4096
TREE_TASKS = 2048
TREE_MEM_MB = 32768
PAGE = os.sysconf("SC_PAGE_SIZE")


def setting(name, default, low, high):
    """An integer environment override in [low, high], else the default."""
    raw = os.environ.get(name, "")
    if raw.isdigit() and low <= int(raw) <= high:
        return int(raw)
    return default


def user_tasks():
    """Tasks (threads included) of processes owned by this user, as RLIMIT_NPROC counts them."""
    uid = os.getuid()
    n = 0
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            if os.stat(f"/proc/{d}").st_uid == uid:
                n += len(os.listdir(f"/proc/{d}/task"))
        except OSError:
            continue
    return n


def stat_of(pid):
    """(state, ppid, comm) of a process, or None when it is gone."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            stat = fh.read().decode(errors="replace")
    except OSError:
        return None
    comm, rest = stat.split("(", 1)[1].rsplit(")", 1)
    fields = rest.split()
    return fields[0], int(fields[1]), comm


def live(pid, state):
    """True unless every thread of the process is a zombie.

    A thread-group leader whose main thread has exited reads `Z` while its
    other threads still run (and can still fork), so `Z` alone is not death.
    """
    if state != "Z":
        return True
    try:
        tids = os.listdir(f"/proc/{pid}/task")
    except OSError:
        return False
    for tid in tids:
        try:
            with open(f"/proc/{pid}/task/{tid}/stat", "rb") as fh:
                if fh.read().decode(errors="replace").rsplit(")", 1)[1].split()[0] != "Z":
                    return True
        except (OSError, IndexError):
            continue
    return False


def children(pid):
    """Child pids, from /proc/<pid>/task/*/children; None on a kernel without that file."""
    files = glob.glob(f"/proc/{pid}/task/*/children")
    if not files:
        return None if os.path.exists(f"/proc/{pid}") else []
    out = []
    for path in files:
        try:
            with open(path) as fh:
                out.extend(int(p) for p in fh.read().split())
        except OSError:
            continue
    return out


def children_by_scan(pid):
    """Child pids by reading every process's parent (kernels without CONFIG_PROC_CHILDREN)."""
    out = []
    for d in os.listdir("/proc"):
        if d.isdigit():
            info = stat_of(int(d))
            if info and info[1] == pid:
                out.append(int(d))
    return out


def kids(pid):
    found = children(pid)
    return children_by_scan(pid) if found is None else found


def attached(me):
    """Live processes whose parent is this process, as (pid, comm)."""
    out = []
    for pid in sorted(kids(me)):
        info = stat_of(pid)
        if info and info[1] == me and live(pid, info[0]):
            out.append((pid, info[2]))
    return out


def walk(roots):
    """Every live process in the trees under `roots`, as {pid: comm}."""
    found, stack = {}, list(roots)
    while stack:
        pid = stack.pop()
        if pid in found:
            continue
        info = stat_of(pid)
        if info is None or not live(pid, info[0]):
            continue
        found[pid] = info[2]
        stack.extend(kids(pid))
    return found


def usage(pids):
    """(tasks, resident MiB) of these processes together."""
    tasks, pages = 0, 0
    for pid in pids:
        try:
            tasks += len(os.listdir(f"/proc/{pid}/task"))
            with open(f"/proc/{pid}/statm") as fh:
                pages += int(fh.read().split()[1])
        except (OSError, IndexError, ValueError):
            continue
    return tasks, pages * PAGE // (1024 * 1024)


def send(pid, sig):
    try:
        os.kill(pid, sig)
        return True
    except OSError:  # gone, or not ours to signal (counted as remaining if it stays)
        return False


def kill_trees(roots):
    """SIGSTOP the trees under `roots` until no new process appears, then SIGKILL them all.

    Returns {pid: comm} of every process that was sent SIGKILL.
    """
    stopped = {}
    for _ in range(64):
        new = {p: c for p, c in walk(roots).items() if p not in stopped}
        if not new:
            break
        for pid in new:
            send(pid, signal.SIGSTOP)
        stopped.update(new)
    return {pid: comm for pid, comm in stopped.items() if send(pid, signal.SIGKILL)}


def tty_foreground():
    """True when this process is in its controlling terminal's foreground group."""
    try:
        fd = os.open("/dev/tty", os.O_RDONLY | os.O_NOCTTY)
    except OSError:
        return False
    try:
        return os.tcgetpgrp(fd) == os.getpgrp()
    except OSError:
        return False
    finally:
        os.close(fd)


def main(argv):
    if len(argv) < 4 or argv[2] != "--":
        print("usage: codex_contain.py <report-file> -- <command> [args...]", file=sys.stderr)
        return 64
    report, cmd = argv[1], argv[3:]
    tree_tasks = setting("CODEX_CONTAIN_TREE_TASKS", TREE_TASKS, 64, 65536)
    tree_mem = setting("CODEX_CONTAIN_TREE_MEM_MB", TREE_MEM_MB, 64, 1048576)

    def write(line):
        try:
            with open(report, "w") as fh:
                fh.write(line + "\n")
        except OSError as e:
            print(f"codex_contain: cannot write {report}: {e}", file=sys.stderr)

    # An inherited SIG_IGN for SIGCHLD would make the kernel reap the command
    # itself, and its exit status could never be read.
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        write("subreaper=off reaped_during=0 reaped_after=0 remaining=0 budget=ok names=")
        print("codex_contain: PR_SET_CHILD_SUBREAPER failed; not running the command", file=sys.stderr)
        return 70
    me = os.getpid()
    parent = os.getppid()
    stop = []
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda signum, _frame: stop.append(signum))
    if not tty_foreground():
        try:
            os.setpgid(0, 0)
        except OSError:
            pass  # a session leader cannot move; it is out of the caller's group already
    cap = user_tasks() + setting("CODEX_CONTAIN_NPROC_HEADROOM", NPROC_HEADROOM, 64, 65536)
    try:
        child = os.fork()
    except OSError as e:
        write("subreaper=on reaped_during=0 reaped_after=0 remaining=0 budget=ok names=")
        print(f"codex_contain: fork failed: {e}", file=sys.stderr)
        return 70
    if child == 0:
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGPIPE, signal.SIGXFSZ):
            signal.signal(sig, signal.SIG_DFL)
        try:
            _soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
            limit = cap if hard == resource.RLIM_INFINITY else min(cap, hard)
            resource.setrlimit(resource.RLIMIT_NPROC, (limit, limit))
        except (OSError, ValueError) as e:
            print(f"codex_contain: cannot cap tasks: {e}; not running the command", file=sys.stderr)
            os._exit(70)
        try:
            os.execvp(cmd[0], cmd)
        except OSError as e:
            print(f"codex_contain: cannot run {cmd[0]}: {e}", file=sys.stderr)
        os._exit(70)

    status = None
    during, after = set(), set()
    names = set()
    budget = "ok"

    def reap():
        nonlocal status
        while True:
            try:
                pid, st = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            if pid == 0:
                return
            if pid == child:
                status = st

    def kill_now(roots):
        killed = kill_trees(roots)
        during.update(killed)
        names.update(killed.values())

    forwarded_at = None
    orphaned = False
    while status is None:
        reap()
        if status is not None:
            break
        if not orphaned and os.getppid() != parent:
            orphaned = True
            print("codex_contain: the caller is gone; killing the command's tree", file=sys.stderr)
            kill_now([pid for pid, _comm in attached(me)])
            continue
        if stop and forwarded_at is None:
            forwarded_at = time.monotonic()
            send(child, signal.SIGTERM)
        elif forwarded_at is not None and time.monotonic() - forwarded_at > AFTER_SECONDS:
            send(child, signal.SIGKILL)
        orphans = [pid for pid, _comm in attached(me) if pid != child]
        if orphans:
            # Confirm the command is still running AFTER listing: when it ends, its own
            # children are re-parented here before reap() sees it, and those belong to
            # the after-end pass, not to "killed while the command ran".
            reap()
            if status is not None:
                break
            kill_now(orphans)
        if budget == "ok":
            tasks, mem = usage(walk([child]))
            if tasks > tree_tasks or mem > tree_mem:
                budget = "tasks" if tasks > tree_tasks else "memory"
                print(
                    f"codex_contain: the command's tree holds {tasks} tasks and {mem} MiB, over its"
                    f" budget of {tree_tasks} tasks / {tree_mem} MiB; killing it",
                    file=sys.stderr,
                )
                kill_now([pid for pid, _comm in attached(me)])
                continue
        time.sleep(POLL_SECONDS)

    deadline = time.monotonic() + AFTER_SECONDS
    while True:
        reap()
        roots = [pid for pid, _comm in attached(me)]
        if not roots:
            break
        killed = kill_trees(roots)
        after.update(pid for pid in killed if pid not in during)
        names.update(killed.values())
        if time.monotonic() >= deadline:
            break
        time.sleep(0.2)
    settle = time.monotonic() + 2.0
    while True:
        reap()
        left = walk([pid for pid, _comm in attached(me)])
        if not left or time.monotonic() >= settle:
            break
        time.sleep(0.1)
    names.update(left.values())
    # A process names itself (prctl PR_SET_NAME), so a name is untrusted text:
    # only [A-Za-z0-9._-] survive, which keeps the report one line of plain words.
    shown = ",".join(sorted(re.sub(r"[^A-Za-z0-9._-]", "_", n) for n in names))[:200]
    write(
        f"subreaper=on reaped_during={len(during)} reaped_after={len(after)}"
        f" remaining={len(left)} budget={budget} names={shown}"
    )
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    return 128 + os.WTERMSIG(status)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
