"""Bounded daemon workers for synchronous network operations."""

from __future__ import annotations

import math
import queue
import socket
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum, auto
from typing import Generic, TypeVar, cast


T = TypeVar("T")
S = TypeVar("S")


class CallDeadlineExceeded(TimeoutError):
    pass


class PoolSaturatedError(TimeoutError):
    pass


class PoolUnavailableError(RuntimeError):
    pass


def validate_deadline(deadline: float) -> float:
    if type(deadline) is not float:
        raise TypeError("deadline must be an exact float")
    if not math.isfinite(deadline):
        raise ValueError("deadline must be finite")

    now = time.monotonic()
    if deadline > now and deadline - now > threading.TIMEOUT_MAX:
        raise ValueError("deadline exceeds the maximum wait duration")
    return deadline


def _deadline_error() -> CallDeadlineExceeded:
    return CallDeadlineExceeded("Network call deadline exceeded.")


def _saturated_error() -> PoolSaturatedError:
    return PoolSaturatedError("Network call pool is saturated.")


def _startup_error() -> PoolUnavailableError:
    return PoolUnavailableError("Network call pool startup failed.")


def _closed_error() -> PoolUnavailableError:
    return PoolUnavailableError("Network call pool is closed.")


def _worker_error() -> PoolUnavailableError:
    return PoolUnavailableError("Network call pool worker stopped.")


@dataclass(frozen=True, slots=True)
class CallOutcome(Generic[T]):
    succeeded: bool
    value: T | None = None
    error: BaseException | None = None

    def __post_init__(self) -> None:
        if type(self.succeeded) is not bool:
            raise TypeError("CallOutcome.succeeded must be an exact bool.")
        if self.succeeded:
            if self.error is not None:
                raise ValueError("A successful outcome cannot contain an error.")
            return
        if self.value is not None:
            raise ValueError("A failed outcome cannot contain a value.")
        if not isinstance(self.error, BaseException):
            raise ValueError("A failed outcome must contain an exception.")

    @classmethod
    def success(cls, value: T) -> "CallOutcome[T]":
        return cls(succeeded=True, value=value)

    @classmethod
    def failure(cls, error: BaseException) -> "CallOutcome[T]":
        return cls(succeeded=False, error=error)

    def result(self) -> T:
        if self.succeeded:
            return cast(T, self.value)
        assert self.error is not None
        raise self.error


class _CallState(Enum):
    PENDING = auto()
    RUNNING = auto()
    COMPLETED = auto()
    ABANDONED_PENDING = auto()
    ABANDONED_RUNNING = auto()
    CONSUMED = auto()


class _AbandonmentReason(Enum):
    DEADLINE = auto()
    INTERRUPTED = auto()
    WORKER_DIED = auto()
    CLOSED = auto()


class BoundedCall(Generic[T]):
    def __init__(self, pool: "BoundedDaemonPool", token: object) -> None:
        self._pool = pool
        self._token = token
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._state = _CallState.PENDING
        self._abandonment: _AbandonmentReason | None = None
        self._outcome: CallOutcome[T] | None = None

    def result_until(self, deadline: float) -> T:
        deadline = validate_deadline(deadline)
        outcome = None
        value = None
        error = None
        reason = None
        try:
            remaining = max(0.0, deadline - time.monotonic())
            self._event.wait(remaining)
            with self._pool._state_lock:
                with self._lock:
                    if self._state is _CallState.COMPLETED:
                        outcome = self._outcome
                        self._outcome = None
                        self._state = _CallState.CONSUMED
                    elif self._state in (
                        _CallState.PENDING,
                        _CallState.RUNNING,
                    ):
                        self._pool._abandon_locked(
                            self,
                            _AbandonmentReason.DEADLINE,
                        )
                        reason = _AbandonmentReason.DEADLINE
                    elif self._state in (
                        _CallState.ABANDONED_PENDING,
                        _CallState.ABANDONED_RUNNING,
                    ):
                        reason = self._abandonment
                    else:
                        raise RuntimeError(
                            "Network call result was already consumed."
                        )

            if outcome is not None:
                if outcome.succeeded:
                    value = outcome.value
                    outcome = None
                    return cast(T, value)
                error = outcome.error
                outcome = None
                assert error is not None
                raise error
            self._raise_abandonment(reason)
            raise AssertionError("unreachable")
        except BaseException:
            outcome = None
            value = None
            error = None
            reason = None
            self._pool._interrupt_call(self)
            raise
        finally:
            outcome = None
            value = None
            error = None
            reason = None

    @staticmethod
    def _raise_abandonment(reason: _AbandonmentReason | None) -> None:
        if reason is _AbandonmentReason.DEADLINE:
            raise _deadline_error()
        if reason is _AbandonmentReason.CLOSED:
            raise _closed_error()
        if reason is _AbandonmentReason.WORKER_DIED:
            raise _worker_error()
        raise PoolUnavailableError("Network call was interrupted.")


@dataclass(slots=True)
class _Task(Generic[T]):
    call: BoundedCall[T]
    function: Callable[..., T]
    args: tuple
    kwargs: dict


_WORKER_STOP = object()


class BoundedDaemonPool:
    def __init__(self, capacity: int, thread_name: str) -> None:
        if type(capacity) is not int:
            raise TypeError("capacity must be an exact int")
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if type(thread_name) is not str:
            raise TypeError("thread_name must be an exact str")
        if not thread_name:
            raise ValueError("thread_name must not be empty")

        self._capacity = capacity
        self._thread_name = thread_name
        self._work_queue = queue.Queue(maxsize=capacity)
        tokens = {object() for _index in range(capacity)}
        self._available_tokens = set(tokens)
        self._token_owners: dict[object, BoundedCall | None] = {
            token: None for token in tokens
        }
        self._workers: list[threading.Thread] = []
        self._active: set[BoundedCall] = set()
        self._startup_attempted = False
        self._startup_succeeded = False
        self._closed = False
        self._close_complete = False
        self._state_lock = threading.Lock()
        self._completion_condition = threading.Condition()
        self._completion_generation = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def startup_failed(self) -> bool:
        with self._state_lock:
            return self._startup_attempted and not self._startup_succeeded

    def ensure_started(self) -> None:
        with self._state_lock:
            if self._closed:
                raise _closed_error()
            if self._startup_attempted:
                if not self._startup_succeeded:
                    raise _startup_error()
                return

            self._startup_attempted = True
            for _index in range(self._capacity):
                worker = threading.Thread(
                    target=self._run,
                    name=self._thread_name,
                    daemon=True,
                )
                self._workers.append(worker)
                worker.start()
            self._startup_succeeded = True

    def submit(
        self,
        function: Callable[..., T],
        /,
        *args,
        **kwargs,
    ) -> BoundedCall[T]:
        self.ensure_started()
        return self._submit_started(function, args, kwargs)

    def _submit_started(
        self,
        function: Callable[..., T],
        args: tuple,
        kwargs: dict,
        *,
        on_enqueued: Callable[[BoundedCall[T]], None] | None = None,
    ) -> BoundedCall[T]:
        with self._state_lock:
            if self._closed:
                raise _closed_error()
            if not self._startup_succeeded:
                raise _startup_error()
            if not self._available_tokens:
                raise _saturated_error()

            token = next(iter(self._available_tokens))
            call = None
            try:
                self._available_tokens.remove(token)
                call = BoundedCall[T](self, token)
                self._token_owners[token] = call
                task = _Task(call, function, args, kwargs)
                self._active.add(call)
                self._work_queue.put_nowait(task)
                if on_enqueued is not None:
                    on_enqueued(call)
                return call
            except queue.Full:
                try:
                    self._cancel_submission_locked(call, token)
                finally:
                    self._cancel_submission_locked(call, token)
                raise _saturated_error() from None
            except BaseException:
                try:
                    self._cancel_submission_locked(call, token)
                finally:
                    self._cancel_submission_locked(call, token)
                raise

    def _cancel_submission_locked(
        self,
        call: BoundedCall | None,
        token: object,
    ) -> None:
        if call is None:
            if self._token_owners[token] is None:
                self._available_tokens.add(token)
            return
        with call._lock:
            self._abandon_locked(call, _AbandonmentReason.INTERRUPTED)
            if self._token_owners[token] is None:
                self._available_tokens.add(token)

    def call_until(
        self,
        deadline: float,
        function: Callable[..., T],
        /,
        *args,
        **kwargs,
    ) -> T:
        deadline = validate_deadline(deadline)
        return self.submit(function, *args, **kwargs).result_until(deadline)

    def map_until(
        self,
        deadline: float,
        function: Callable[[S], T],
        items: Iterable[S],
        /,
    ) -> list[CallOutcome[T]]:
        deadline = validate_deadline(deadline)
        item_list = list(items)
        if not item_list:
            return []
        self.ensure_started()

        outcomes: list[CallOutcome[T] | None] = [None] * len(item_list)
        active: dict[int, BoundedCall[T]] = {}
        next_index = 0
        try:
            while next_index < len(item_list) or active:
                while (
                    next_index < len(item_list)
                    and len(active) < self._capacity
                    and time.monotonic() < deadline
                ):
                    index = next_index
                    next_index += 1

                    def register_call(
                        submitted_call: BoundedCall[T],
                        *,
                        index: int = index,
                    ) -> None:
                        active[index] = submitted_call

                    try:
                        self._submit_started(
                            function,
                            (item_list[index],),
                            {},
                            on_enqueued=register_call,
                        )
                    except (PoolSaturatedError, PoolUnavailableError) as error:
                        outcomes[index] = CallOutcome.failure(error)

                with self._completion_condition:
                    generation = self._completion_generation

                made_progress = False
                for index, call in tuple(active.items()):
                    if not call._event.is_set():
                        continue
                    outcomes[index] = self._call_outcome(call, deadline)
                    del active[index]
                    made_progress = True
                call = None

                if made_progress:
                    continue
                if not active:
                    if next_index >= len(item_list):
                        break
                    if time.monotonic() >= deadline:
                        break
                    continue

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    for index, call in tuple(active.items()):
                        outcomes[index] = self._call_outcome(call, deadline)
                        del active[index]
                    call = None
                    break

                with self._completion_condition:
                    if generation == self._completion_generation:
                        self._completion_condition.wait(remaining)

            for index in range(next_index, len(item_list)):
                outcomes[index] = CallOutcome.failure(_deadline_error())
            for index, outcome in enumerate(outcomes):
                if outcome is None:
                    outcomes[index] = CallOutcome.failure(_deadline_error())
            return cast(list[CallOutcome[T]], outcomes)
        except BaseException:
            self._interrupt_calls(tuple(active.values()))
            active.clear()
            outcomes.clear()
            item_list.clear()
            call = None
            raise

    @staticmethod
    def _call_outcome(
        call: BoundedCall[T],
        deadline: float,
    ) -> CallOutcome[T]:
        value = None
        outcome = None
        error = None
        try:
            try:
                value = call.result_until(deadline)
            except Exception as operation_error:
                error = operation_error
                outcome = CallOutcome.failure(error)
            else:
                outcome = CallOutcome.success(value)
            return cast(CallOutcome[T], outcome)
        except BaseException:
            value = None
            outcome = None
            error = None
            raise
        finally:
            value = None
            outcome = None
            error = None

    def begin_close(self) -> None:
        with self._state_lock:
            if self._close_complete:
                return
            self._closed = True

            for call in tuple(self._active):
                with call._lock:
                    self._abandon_locked(call, _AbandonmentReason.CLOSED)

            while True:
                try:
                    task = self._work_queue.get_nowait()
                except queue.Empty:
                    break
                if task is _WORKER_STOP:
                    continue
                call = task.call
                with call._lock:
                    self._abandon_locked(call, _AbandonmentReason.CLOSED)
                    self._return_token_locked(call)
                task = None
                call = None

            workers = tuple(
                worker
                for worker in self._workers
                if getattr(worker, "ident", None) is not None
                and worker.is_alive()
            )
            for _worker in workers:
                self._work_queue.put_nowait(_WORKER_STOP)
            self._close_complete = True

    def join_until(self, deadline: float) -> None:
        with self._state_lock:
            workers = tuple(self._workers)
        for worker in workers:
            if getattr(worker, "ident", None) is None:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            worker.join(remaining)

    def close_until(self, deadline: float) -> None:
        self.begin_close()
        self.join_until(deadline)

    def _run(self) -> None:
        task = None
        call = None
        function = None
        args = None
        kwargs = None
        outcome = None
        value = None
        while True:
            try:
                task = self._work_queue.get()
                if task is _WORKER_STOP:
                    return
                call = task.call
                if not self._claim(call):
                    continue

                function = task.function
                args = task.args
                kwargs = task.kwargs
                value = None
                try:
                    value = function(*args, **kwargs)
                except Exception as error:
                    outcome = CallOutcome.failure(error)
                else:
                    outcome = CallOutcome.success(value)

                self._finish(call, outcome)
            except BaseException:
                if task is not None and task is not _WORKER_STOP:
                    try:
                        self._worker_died(task.call)
                    finally:
                        self._worker_died(task.call)
                return
            finally:
                task = None
                call = None
                function = None
                args = None
                kwargs = None
                outcome = None
                value = None

    def _claim(self, call: BoundedCall) -> bool:
        with self._state_lock:
            with call._lock:
                if call._state is _CallState.PENDING and not self._closed:
                    call._state = _CallState.RUNNING
                    return True
                if call._state is _CallState.PENDING:
                    self._abandon_locked(call, _AbandonmentReason.CLOSED)
                self._return_token_locked(call)
                return False

    def _finish(
        self,
        call: BoundedCall[T],
        outcome: CallOutcome[T],
    ) -> None:
        with self._state_lock:
            with call._lock:
                if call._state is _CallState.RUNNING:
                    call._outcome = outcome
                    call._state = _CallState.COMPLETED
                if call._state is _CallState.COMPLETED:
                    self._signal_locked(call)
                self._return_token_locked(call)

    def _worker_died(self, call: BoundedCall) -> None:
        with self._state_lock:
            with call._lock:
                if call._state is _CallState.RUNNING:
                    call._outcome = None
                if call._state in (
                    _CallState.PENDING,
                    _CallState.RUNNING,
                    _CallState.ABANDONED_PENDING,
                    _CallState.ABANDONED_RUNNING,
                ):
                    self._abandon_locked(
                        call,
                        _AbandonmentReason.WORKER_DIED,
                    )
                elif call._state is _CallState.COMPLETED:
                    self._signal_locked(call)
                self._return_token_locked(call)

    def _interrupt_call(self, call: BoundedCall) -> None:
        self._interrupt_calls((call,))

    def _interrupt_calls(self, calls: Iterable[BoundedCall]) -> None:
        for call in calls:
            with self._state_lock:
                with call._lock:
                    if call._state in (
                        _CallState.PENDING,
                        _CallState.RUNNING,
                        _CallState.ABANDONED_PENDING,
                        _CallState.ABANDONED_RUNNING,
                    ):
                        self._abandon_locked(
                            call,
                            _AbandonmentReason.INTERRUPTED,
                        )
                    elif call._state is _CallState.COMPLETED:
                        call._outcome = None
                        call._state = _CallState.CONSUMED

    def _abandon_locked(
        self,
        call: BoundedCall,
        reason: _AbandonmentReason,
    ) -> None:
        if call._state is _CallState.PENDING:
            abandoned_state = _CallState.ABANDONED_PENDING
        elif call._state is _CallState.RUNNING:
            abandoned_state = _CallState.ABANDONED_RUNNING
        elif call._state in (
            _CallState.ABANDONED_PENDING,
            _CallState.ABANDONED_RUNNING,
        ):
            abandoned_state = call._state
        else:
            return
        call._state = abandoned_state
        if call._abandonment is None:
            call._abandonment = reason
        self._signal_locked(call)
        if abandoned_state is _CallState.ABANDONED_PENDING:
            self._return_token_locked(call)

    def _signal_locked(self, call: BoundedCall) -> None:
        call._event.set()
        with self._completion_condition:
            self._completion_generation += 1
            self._completion_condition.notify_all()

    def _return_token_locked(self, call: BoundedCall) -> None:
        token = call._token
        if self._token_owners[token] is call:
            # Publish capacity first so an interrupted return remains repairable.
            self._available_tokens.add(token)
            self._token_owners[token] = None
        self._active.discard(call)


@dataclass(frozen=True, slots=True)
class NetworkCallPools:
    dns: BoundedDaemonPool
    http: BoundedDaemonPool
    icmp: BoundedDaemonPool


_NETWORK_CALL_POOLS = NetworkCallPools(
    dns=BoundedDaemonPool(4, "obozrenie-dns-resolver"),
    http=BoundedDaemonPool(4, "obozrenie-http"),
    icmp=BoundedDaemonPool(32, "obozrenie-icmp"),
)


def get_network_call_pools() -> NetworkCallPools:
    return _NETWORK_CALL_POOLS


def close_network_call_pools(timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    pools = get_network_call_pools()
    ordered_pools = (pools.dns, pools.http, pools.icmp)
    first_error = None
    incomplete_pools = []
    for pool in ordered_pools:
        try:
            pool.begin_close()
        except BaseException as error:
            if first_error is None:
                first_error = error
            if not getattr(pool, "_close_complete", False):
                incomplete_pools.append(pool)
        else:
            if getattr(pool, "_close_complete", True) is False:
                incomplete_pools.append(pool)
    for pool in incomplete_pools:
        try:
            pool.begin_close()
        except BaseException as error:
            if first_error is None:
                first_error = error
    for pool in ordered_pools:
        try:
            pool.join_until(deadline)
        except BaseException as error:
            if first_error is None:
                first_error = error
    if first_error is not None:
        raise first_error


def getaddrinfo_many_until(
    hosts: Iterable[str],
    deadline: float,
) -> list[CallOutcome[list[tuple]]]:
    deadline = validate_deadline(deadline)
    host_list = list(hosts)
    if not host_list:
        return []

    pool = get_network_call_pools().dns
    try:
        pool.ensure_started()
    except BaseException:
        return [
            CallOutcome.failure(_startup_error()) for _host in host_list
        ]

    def resolve(host: str) -> list[tuple]:
        return socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)

    try:
        return pool.map_until(deadline, resolve, host_list)
    except PoolUnavailableError as error:
        if type(error) is not PoolUnavailableError:
            raise
        return [
            CallOutcome.failure(_closed_error()) for _host in host_list
        ]
