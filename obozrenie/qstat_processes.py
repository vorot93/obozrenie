"""Ownership of QStat child processes from spawn registration to reaping."""

from __future__ import annotations

import subprocess
import threading
import time

from obozrenie.network_calls import validate_deadline


# One reaping round observes each retained child for this long, and backs off
# for the same bound when a round released nothing.
_REAP_WAIT_SECONDS = 0.05
# An idle reaper is woken by every registration and by close; this bound only
# makes it re-check state that no notification could have reached.
_IDLE_WAIT_SECONDS = 1.0
_REAPER_THREAD_NAME = "obozrenie-qstat-reaper"


class QStatSupervisorClosedError(RuntimeError):
    pass


def _closed_error() -> QStatSupervisorClosedError:
    return QStatSupervisorClosedError("QStat is shutting down.")


def _kill_quietly(process: subprocess.Popen) -> None:
    """Kill one child, tolerating a signal that cannot be delivered.

    The child has usually already gone; whatever else keeps the signal from
    its own child, the handle stays registered and the reaper retains it
    until an exit is observed, so the failure changes nothing about who owns
    the child and must not replace the reason its caller already has —
    close, or a spawn that lost the race against it.
    """
    try:
        process.kill()
    except OSError:
        pass


def _close_pipes(process: subprocess.Popen) -> None:
    """Close the parent ends of a child's pipes, if it opened any.

    Only a child no caller will ever receive is closed here, so a pipe that
    fails to close is not a failure anyone can act on: the other pipes are
    still closed, and the caller keeps the reason it already has.
    """
    for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass


def _exit_observed(process: subprocess.Popen) -> bool:
    """Report whether this child's exit has been observed, never waiting long.

    A handle whose status cannot be read stays retained: the supervisor drops
    a child only once it has seen that child exit.
    """
    try:
        if process.poll() is not None:
            return True
        process.wait(timeout=_REAP_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        return False
    except OSError:
        return False
    return True


class QStatProcessSupervisor:
    """Own every QStat child handle from spawn registration through reaping.

    Spawn registration and close share one lock, so no child can be born
    between the closing check and its registration. `Popen` itself runs
    outside that lock: a reservation publishes the in-flight spawn, and the
    returned handle replaces it. Whichever side loses the race against close
    kills the child it registered, so a child is never lost while it lives.

    Reaping is bounded everywhere. A caller that cannot reap its child within
    its own deadline defers the handle to the supervisor's single daemon
    reaper, which retains it until `poll()` or a finite `wait()` proves exit.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._reservations: set[object] = set()
        self._processes: set[subprocess.Popen] = set()
        self._pending: set[subprocess.Popen] = set()
        self._closing = False
        self._close_complete = False
        self._reaper: threading.Thread | None = None
        self._reaper_start_attempted = False

    @property
    def registered_processes(self) -> tuple[subprocess.Popen, ...]:
        """Return every handle this supervisor still owns."""
        with self._lock:
            return tuple(self._processes)

    def spawn(self, command: list[str], **popen_kwargs) -> subprocess.Popen:
        """Start one registered child, or raise once close has begun."""
        reservation = object()
        with self._lock:
            if self._closing:
                raise _closed_error()
            self._reservations.add(reservation)

        process = None
        closing = False
        abandoned = False
        try:
            # Never hold the lock across process creation: it can block, and
            # close must stay bounded while a spawn is in flight.
            process = subprocess.Popen(command, **popen_kwargs)
            with self._lock:
                self._processes.add(process)
                closing = self._closing
                if closing:
                    self._pending.add(process)
        finally:
            # However this spawn ends, its reservation goes with it: one that
            # outlived it would hold the reaper open for a child that can
            # never arrive.
            with self._lock:
                self._reservations.discard(reservation)
                # An interruption between `Popen` and the registration above
                # would otherwise abandon a living child: adopt it here, so
                # the reaper owns every handle this method created.
                abandoned = (
                    process is not None and process not in self._processes
                )
                if abandoned:
                    self._processes.add(process)
                    self._pending.add(process)
                self._idle.notify_all()
            if abandoned:
                self._discard_child(process)

        if not closing:
            return process

        # Close swept the registry before this child was registered, so this
        # caller kills it: registered first, killed second, never lost.
        self._discard_child(process)
        raise _closed_error()

    def _discard_child(self, process: subprocess.Popen) -> None:
        """Stop and retain a child no caller will ever receive.

        Nothing will ever use this child and no caller received the handle,
        so it is stopped and its pipes closed here, each step tolerating its
        own failure; the reaper takes it from there — whatever the stop or
        the closes did, since a child that is registered but never reaped
        would be exactly the lost handle this supervisor exists to prevent.
        """
        try:
            _kill_quietly(process)
            _close_pipes(process)
        finally:
            self._ensure_reaper()

    def defer_reap(self, process: subprocess.Popen) -> None:
        """Hand one child that outlived its caller's bound to the reaper."""
        with self._lock:
            self._processes.add(process)
            self._pending.add(process)
            self._idle.notify_all()
        self._ensure_reaper()

    def release_reaped(self, process: subprocess.Popen) -> None:
        """Forget one child whose exit has already been observed."""
        if process.returncode is None:
            raise ValueError("A running QStat child cannot be released.")
        with self._lock:
            self._processes.discard(process)
            self._pending.discard(process)
            self._idle.notify_all()

    def begin_close(self) -> None:
        """Reject new children, kill every registered one, and start reaping."""
        with self._lock:
            if self._close_complete:
                return
            self._closing = True
            processes = tuple(self._processes)
            # Every surviving handle becomes the reaper's, so an interrupted
            # or blocked query cannot strand a child that no one reaps.
            self._pending.update(processes)
            self._idle.notify_all()

        # Signalling a child is a system call on a foreign handle: it runs
        # outside the lock, where a spawn racing close can still register.
        for process in processes:
            _kill_quietly(process)

        with self._lock:
            self._close_complete = True
            pending = bool(self._pending)
        if pending:
            self._ensure_reaper()

    def join_until(self, deadline: float) -> None:
        """Join the daemon reaper against one absolute deadline."""
        deadline = validate_deadline(deadline)
        with self._lock:
            reaper = self._reaper
        if reaper is None or reaper.ident is None:
            # A reaper that never launched cannot be joined. Its handles stay
            # registered rather than being silently dropped.
            return
        remaining = deadline - time.monotonic()
        if remaining > 0:
            reaper.join(remaining)

    def close_until(self, deadline: float) -> None:
        """Close the supervisor and join its reaper against one deadline."""
        # Validate first: a malformed deadline must not half-close.
        deadline = validate_deadline(deadline)
        self.begin_close()
        self.join_until(deadline)

    def _ensure_reaper(self) -> None:
        """Start this supervisor's one daemon reaper, at most once."""
        with self._lock:
            if self._reaper_start_attempted:
                return
            # One shot: a `start()` that raises may still have launched the
            # thread, so the attempt is recorded before it is made and is
            # never repeated. A second reaper could reap another's child.
            self._reaper_start_attempted = True
            reaper = threading.Thread(
                target=self._reap_retained_children,
                name=_REAPER_THREAD_NAME,
                daemon=True,
            )
            self._reaper = reaper
        try:
            reaper.start()
        except RuntimeError:
            # Reaping is best effort; retention is the contract. Without a
            # reaper the handles simply stay registered, and the thread that
            # may yet be running is still the one `join_until` joins.
            pass

    def _reap_retained_children(self) -> None:
        """Retain every deferred child until its own exit is observed."""
        while True:
            with self._lock:
                while not self._pending:
                    if self._closing and not self._reservations:
                        # Closed, nothing retained, and no spawn can still
                        # deliver a child: no more work can reach this reaper.
                        return
                    # Nothing to observe yet. Every registration, release and
                    # close notifies, so this bound only re-checks state that
                    # no notification could have reached.
                    self._idle.wait(_IDLE_WAIT_SECONDS)
                pending = tuple(self._pending)

            reaped = [process for process in pending if _exit_observed(process)]
            for process in reaped:
                self.release_reaped(process)
            if len(reaped) < len(pending):
                with self._lock:
                    self._idle.wait(_REAP_WAIT_SECONDS)


_QSTAT_PROCESS_SUPERVISOR = QStatProcessSupervisor()


def get_qstat_process_supervisor() -> QStatProcessSupervisor:
    """Return the supervisor that owns this application's QStat children."""
    return _QSTAT_PROCESS_SUPERVISOR


def install_qstat_process_supervisor(
    supervisor: QStatProcessSupervisor,
) -> QStatProcessSupervisor:
    """Install one supervisor and return the one it replaced."""
    global _QSTAT_PROCESS_SUPERVISOR

    previous = _QSTAT_PROCESS_SUPERVISOR
    _QSTAT_PROCESS_SUPERVISOR = supervisor
    return previous
