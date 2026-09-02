import contextlib
import gc
import inspect
import queue
import subprocess
import sys
import threading
import time
import unittest
import weakref
from types import SimpleNamespace
from unittest import mock

from obozrenie import network_calls
from obozrenie.network_calls import (
    BoundedCall,
    CallDeadlineExceeded,
    CallOutcome,
    PoolSaturatedError,
    PoolUnavailableError,
)


class _CallerAbort(BaseException):
    pass


class _WorkerAbort(BaseException):
    pass


class _LargeValue:
    __slots__ = ("payload", "__weakref__")

    def __init__(self):
        self.payload = bytearray(1024 * 1024)


class _FloatDeadline(float):
    pass


@contextlib.contextmanager
def installed_network_pools():
    fresh = network_calls.NetworkCallPools(
        dns=network_calls.BoundedDaemonPool(4, "obozrenie-dns-resolver"),
        http=network_calls.BoundedDaemonPool(4, "obozrenie-http"),
        icmp=network_calls.BoundedDaemonPool(32, "obozrenie-icmp"),
    )
    patcher = mock.patch.object(network_calls, "_NETWORK_CALL_POOLS", fresh)
    patcher.start()
    try:
        yield fresh
    finally:
        try:
            network_calls.close_network_call_pools(timeout=0.2)
        finally:
            patcher.stop()


def _capture_error(callback):
    try:
        callback()
    except BaseException as error:
        return error
    raise AssertionError("expected callback to raise")


def _wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return predicate()


def _available_token_count(pool):
    with pool._state_lock:
        return len(pool._available_tokens)


def _token_is_returned(pool, call):
    with pool._state_lock:
        return pool._token_owners[call._token] is not call


@contextlib.contextmanager
def _record_constructed_calls():
    real_call = BoundedCall
    calls = []

    class RecordingCall:
        @classmethod
        def __class_getitem__(cls, _item):
            return cls

        def __new__(cls, *args, **kwargs):
            call = real_call(*args, **kwargs)
            calls.append(call)
            return call

    with mock.patch.object(network_calls, "BoundedCall", RecordingCall):
        yield calls


@contextlib.contextmanager
def _raise_on_statement(
    function,
    statement,
    error,
    *,
    new_threads=False,
    before_raise=None,
):
    lines, first_line = inspect.getsourcelines(function)
    for offset, line in enumerate(lines):
        if line.strip() == statement:
            target_line = first_line + offset
            break
    else:
        raise AssertionError(f"statement not found: {statement}")

    def trace(frame, event, _argument):
        if (
            event == "line"
            and frame.f_code is function.__code__
            and frame.f_lineno == target_line
        ):
            if before_raise is not None:
                before_raise(frame)
            raise error
        return trace

    get_trace = threading.gettrace if new_threads else sys.gettrace
    set_trace = threading.settrace if new_threads else sys.settrace
    previous_trace = get_trace()
    try:
        set_trace(trace)
        yield
    finally:
        set_trace(previous_trace)


@contextlib.contextmanager
def _raise_when(predicate, error):
    def trace(frame, event, _argument):
        if event == "line" and predicate(frame):
            raise error
        return trace

    previous_trace = sys.gettrace()
    try:
        sys.settrace(trace)
        yield
    finally:
        sys.settrace(previous_trace)


@contextlib.contextmanager
def _raise_on_return(function, error, *, new_threads=False):
    def trace(frame, event, _argument):
        if event == "return" and frame.f_code is function.__code__:
            raise error
        return trace

    get_trace = threading.gettrace if new_threads else sys.gettrace
    set_trace = threading.settrace if new_threads else sys.settrace
    previous_trace = get_trace()
    try:
        set_trace(trace)
        yield
    finally:
        set_trace(previous_trace)


@contextlib.contextmanager
def _interrupt_after_outcome_detach(call, error, interrupted_codes):
    def detached_outcome_is_unlocked(frame):
        if (
            frame.f_code is not BoundedCall.result_until.__code__
            or call._state is not network_calls._CallState.CONSUMED
            or call._outcome is not None
            or call._pool._state_lock.locked()
            or call._lock.locked()
        ):
            return False
        interrupted_codes.append(frame.f_code)
        return True

    with _raise_when(detached_outcome_is_unlocked, error):
        yield


class NetworkPoolFixtureTests(unittest.TestCase):
    def test_close_failure_restores_registry_and_cleans_blocked_caller(self):
        original_pools = network_calls.get_network_call_pools()
        real_close = network_calls.close_network_call_pools
        cleanup_error = _CallerAbort("fixture close failed")
        operation_started = threading.Event()
        release_operation = threading.Event()
        operation_returned = threading.Event()
        caller_finished = threading.Event()
        caller_outcomes = []
        pools = None
        caller = None
        raised = None
        registry_after_close = None
        caller_finished_before_release = False
        blocker_returned = False
        workers_stopped = False

        def block():
            operation_started.set()
            try:
                if not release_operation.wait(2):
                    raise AssertionError("test did not release fixture blocker")
            finally:
                operation_returned.set()

        def invoke():
            try:
                value = pools.dns.call_until(
                    time.monotonic() + 10,
                    block,
                )
            except BaseException as error:
                caller_outcomes.append((False, error))
            else:
                caller_outcomes.append((True, value))
            finally:
                caller_finished.set()

        def close_then_abort(timeout):
            real_close(timeout=timeout)
            raise cleanup_error

        try:
            with mock.patch.object(
                network_calls,
                "close_network_call_pools",
                side_effect=close_then_abort,
            ):
                try:
                    with installed_network_pools() as installed_pools:
                        pools = installed_pools
                        caller = threading.Thread(
                            target=invoke,
                            name="test-fixture-close-caller",
                        )
                        caller.start()
                        if not operation_started.wait(1):
                            raise AssertionError(
                                "fixture blocker did not start"
                            )
                except BaseException as error:
                    raised = error

                registry_after_close = network_calls.get_network_call_pools()
                caller_finished_before_release = caller_finished.wait(1)
        finally:
            release_operation.set()
            if caller is not None:
                caller.join(1)
            blocker_returned = operation_returned.wait(1)
            if pools is not None:
                workers_stopped = _wait_until(
                    lambda: all(
                        not worker.is_alive() for worker in pools.dns._workers
                    )
                )
            network_calls._NETWORK_CALL_POOLS = original_pools

        self.assertIs(raised, cleanup_error)
        self.assertIs(registry_after_close, original_pools)
        self.assertTrue(caller_finished_before_release)
        self.assertIsNotNone(caller)
        self.assertFalse(caller.is_alive())
        self.assertEqual(len(caller_outcomes), 1)
        succeeded, caller_error = caller_outcomes[0]
        self.assertFalse(succeeded)
        self.assertIs(type(caller_error), PoolUnavailableError)
        self.assertTrue(blocker_returned)
        self.assertTrue(workers_stopped)


class CallOutcomeTests(unittest.TestCase):
    def test_success_and_failure_preserve_exact_outcomes(self):
        self.assertIsNone(CallOutcome.success(None).result())
        value = _LargeValue()
        self.assertIs(CallOutcome.success(value).result(), value)

        error = RuntimeError("operation failed")
        with self.assertRaises(RuntimeError) as raised:
            CallOutcome.failure(error).result()
        self.assertIs(raised.exception, error)

    def test_constructor_rejects_ambiguous_or_invalid_branches(self):
        error = RuntimeError("operation failed")
        for kwargs in (
            {"succeeded": False},
            {"succeeded": True, "error": error},
            {"succeeded": False, "value": 1, "error": error},
            {"succeeded": 1, "value": "bad"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(
                (TypeError, ValueError)
            ):
                CallOutcome(**kwargs)


class DeadlineValidationTests(unittest.TestCase):
    def invalid_deadlines(self, now=100.0):
        return (
            ("none", None, TypeError),
            ("integer", 101, TypeError),
            ("boolean", True, TypeError),
            ("string", "101.0", TypeError),
            ("float subclass", _FloatDeadline(101.0), TypeError),
            ("not a number", float("nan"), ValueError),
            ("positive infinity", float("inf"), ValueError),
            ("negative infinity", float("-inf"), ValueError),
            (
                "oversized finite future",
                now + (threading.TIMEOUT_MAX * 2.0),
                ValueError,
            ),
        )

    def test_public_validator_rejects_malformed_deadlines_by_exact_family(self):
        fake_time = SimpleNamespace(monotonic=mock.Mock(return_value=100.0))
        for label, deadline, error_type in self.invalid_deadlines():
            with self.subTest(deadline=label), mock.patch.object(
                network_calls,
                "time",
                fake_time,
            ):
                error = _capture_error(
                    lambda deadline=deadline: network_calls.validate_deadline(
                        deadline
                    )
                )

            self.assertIs(type(error), error_type)

    def test_public_validator_returns_exact_finite_deadline_unchanged(self):
        fake_time = SimpleNamespace(monotonic=mock.Mock(return_value=100.0))
        deadlines = (
            float("-1e300"),
            float("100.0"),
            float("101.5"),
            float(100.0 + threading.TIMEOUT_MAX),
        )
        validator = getattr(network_calls, "validate_deadline", None)
        self.assertIsNotNone(validator)
        with mock.patch.object(network_calls, "time", fake_time):
            for deadline in deadlines:
                with self.subTest(deadline=deadline):
                    self.assertIs(validator(deadline), deadline)

    def test_call_until_validates_before_submission_or_startup(self):
        fake_time = SimpleNamespace(monotonic=mock.Mock(return_value=100.0))
        for label, deadline, error_type in self.invalid_deadlines():
            with self.subTest(deadline=label):
                pool = network_calls.BoundedDaemonPool(1, "invalid-call")
                with (
                    mock.patch.object(network_calls, "time", fake_time),
                    mock.patch.object(
                        pool,
                        "submit",
                        side_effect=AssertionError(
                            "invalid call reached submission"
                        ),
                    ) as submit,
                ):
                    error = _capture_error(
                        lambda deadline=deadline: pool.call_until(
                            deadline,
                            lambda: None,
                        )
                    )

                self.assertIs(type(error), error_type)
                submit.assert_not_called()
                self.assertEqual(pool._workers, [])

    def test_map_until_validates_before_materialization_or_startup(self):
        fake_time = SimpleNamespace(monotonic=mock.Mock(return_value=100.0))

        class UnmaterializableItems:
            def __init__(self):
                self.iterations = 0

            def __iter__(self):
                self.iterations += 1
                raise AssertionError("invalid map materialized its items")

        for label, deadline, error_type in self.invalid_deadlines():
            with self.subTest(deadline=label):
                pool = network_calls.BoundedDaemonPool(1, "invalid-map")
                items = UnmaterializableItems()
                with mock.patch.object(network_calls, "time", fake_time):
                    error = _capture_error(
                        lambda deadline=deadline: pool.map_until(
                            deadline,
                            lambda item: item,
                            items,
                        )
                    )

                self.assertIs(type(error), error_type)
                self.assertEqual(items.iterations, 0)
                self.assertEqual(pool._workers, [])

    def test_dns_facade_validates_before_iterating_an_empty_batch(self):
        events = []
        deadline = 101.0

        class SideEffectHosts:
            def __iter__(self):
                events.append("hosts iterated")
                return iter(())

        def validate(supplied_deadline):
            events.append("deadline validated")
            self.assertIs(supplied_deadline, deadline)
            return supplied_deadline

        with (
            mock.patch.object(
                network_calls,
                "validate_deadline",
                side_effect=validate,
            ) as validator,
            mock.patch.object(
                network_calls,
                "get_network_call_pools",
                side_effect=AssertionError(
                    "empty DNS batch resolved the registry"
                ),
            ) as get_pools,
        ):
            outcomes = network_calls.getaddrinfo_many_until(
                SideEffectHosts(),
                deadline,
            )

        self.assertEqual(outcomes, [])
        self.assertEqual(events, ["deadline validated", "hosts iterated"])
        validator.assert_called_once_with(deadline)
        get_pools.assert_not_called()

    def test_dns_facade_rejects_deadline_before_raising_iterable_or_pool(self):
        fake_time = SimpleNamespace(monotonic=mock.Mock(return_value=100.0))

        class RaisingHosts:
            def __init__(self):
                self.iterations = 0

            def __iter__(self):
                self.iterations += 1
                raise AssertionError("invalid DNS batch iterated its hosts")

        for label, deadline, error_type in self.invalid_deadlines():
            with self.subTest(deadline=label):
                hosts = RaisingHosts()
                with (
                    mock.patch.object(network_calls, "time", fake_time),
                    mock.patch.object(
                        network_calls,
                        "get_network_call_pools",
                        side_effect=AssertionError(
                            "invalid DNS deadline resolved the registry"
                        ),
                    ) as get_pools,
                ):
                    error = _capture_error(
                        lambda deadline=deadline: (
                            network_calls.getaddrinfo_many_until(
                                hosts,
                                deadline,
                            )
                        )
                    )

                self.assertIs(type(error), error_type)
                self.assertEqual(hosts.iterations, 0)
                get_pools.assert_not_called()

    def test_result_until_validates_before_event_wait(self):
        fake_time = SimpleNamespace(monotonic=mock.Mock(return_value=100.0))

        class RecordingEvent:
            def __init__(self):
                self.wait_calls = []
                self.set_calls = 0

            def wait(self, timeout=None):
                self.wait_calls.append(timeout)
                raise AssertionError("invalid deadline reached Event.wait")

            def set(self):
                self.set_calls += 1

            def is_set(self):
                return self.set_calls > 0

        for label, deadline, error_type in self.invalid_deadlines():
            with self.subTest(deadline=label):
                pool = network_calls.BoundedDaemonPool(1, "invalid-result")
                pool._startup_attempted = True
                pool._startup_succeeded = True
                call = pool.submit(lambda: None)
                event = RecordingEvent()
                call._event = event
                try:
                    with mock.patch.object(network_calls, "time", fake_time):
                        error = _capture_error(
                            lambda deadline=deadline: call.result_until(
                                deadline
                            )
                        )

                    self.assertIs(type(error), error_type)
                    self.assertEqual(event.wait_calls, [])
                    self.assertEqual(call._state.name, "PENDING")
                    self.assertIs(pool._token_owners[call._token], call)
                    self.assertEqual(_available_token_count(pool), 0)
                finally:
                    pool.begin_close()


class BoundedCallStateTests(unittest.TestCase):
    def _assert_worker_claim_abort_is_cleaned(self, after_claim):
        pool = network_calls.BoundedDaemonPool(1, "claim-abort")
        original_claim = pool._claim
        injected = _WorkerAbort("worker interrupted around claim")
        function_calls = []

        def abort_around_claim(call):
            if after_claim:
                original_claim(call)
            raise injected

        try:
            with mock.patch.object(
                pool,
                "_claim",
                side_effect=abort_around_claim,
            ), mock.patch("threading.excepthook"):
                call = pool.submit(lambda: function_calls.append(True))
                error = _capture_error(
                    lambda: call.result_until(time.monotonic() + 0.05)
                )
                pool._workers[0].join(1)
            before_close = (
                len(pool._active),
                _available_token_count(pool),
                _token_is_returned(pool, call),
            )
        finally:
            pool.close_until(time.monotonic() + 0.2)

        self.assertIs(type(error), PoolUnavailableError)
        self.assertEqual(function_calls, [])
        self.assertEqual(call._abandonment.name, "WORKER_DIED")
        self.assertIsNone(call._outcome)
        self.assertFalse(pool._workers[0].is_alive())
        self.assertEqual(before_close, (0, 1, True))
        self.assertEqual(len(pool._active), 0)
        self.assertEqual(_available_token_count(pool), 1)

    def test_worker_base_exception_before_claim_cleans_dequeued_call(self):
        self._assert_worker_claim_abort_is_cleaned(after_claim=False)

    def test_worker_base_exception_after_claim_cleans_dequeued_call(self):
        self._assert_worker_claim_abort_is_cleaned(after_claim=True)

    def _assert_worker_trace_abort_is_cleaned(
        self,
        statement,
        *,
        operation_aborts=False,
    ):
        pool = network_calls.BoundedDaemonPool(1, "traced-worker-abort")
        injected = _WorkerAbort(f"worker interrupted at {statement}")
        function_calls = []

        def operation():
            function_calls.append(True)
            if operation_aborts:
                raise _WorkerAbort("operation interrupted worker")

        try:
            with _raise_on_statement(
                network_calls.BoundedDaemonPool._run,
                statement,
                injected,
                new_threads=True,
            ), mock.patch("threading.excepthook"):
                call = pool.submit(operation)
                pool._workers[0].join(1)
                first_error = _capture_error(
                    lambda: call.result_until(time.monotonic() + 0.05)
                )
                second_error = _capture_error(
                    lambda: call.result_until(time.monotonic() + 0.05)
                )
            before_close = (
                len(pool._active),
                _available_token_count(pool),
                _token_is_returned(pool, call),
            )
        finally:
            pool.close_until(time.monotonic() + 0.2)

        self.assertIs(type(first_error), PoolUnavailableError)
        self.assertIs(type(second_error), PoolUnavailableError)
        self.assertIsNot(first_error, second_error)
        self.assertEqual(function_calls, [True] if operation_aborts else [])
        self.assertEqual(call._abandonment.name, "WORKER_DIED")
        self.assertIsNone(call._outcome)
        self.assertFalse(pool._workers[0].is_alive())
        self.assertEqual(before_close, (0, 1, True))
        self.assertEqual(len(pool._active), 0)
        self.assertEqual(_available_token_count(pool), 1)

    def test_worker_interruption_after_dequeue_cleans_call(self):
        self._assert_worker_trace_abort_is_cleaned(
            "if task is _WORKER_STOP:",
        )

    def test_worker_interruption_during_call_extraction_cleans_dequeued_call(self):
        self._assert_worker_trace_abort_is_cleaned("call = task.call")

    def test_worker_interruption_during_first_recovery_still_cleans_call(self):
        self._assert_worker_trace_abort_is_cleaned(
            "self._worker_died(task.call)",
            operation_aborts=True,
        )

    def test_completion_published_before_deadline_observation_wins_exactly(self):
        with installed_network_pools() as pools:
            value = _LargeValue()
            value_call = pools.dns.submit(lambda: value)
            self.assertTrue(value_call._event.wait(1))
            self.assertIs(
                value_call.result_until(time.monotonic() - 1),
                value,
            )

            error = RuntimeError("exact operation failure")

            def fail():
                raise error

            error_call = pools.dns.submit(fail)
            self.assertTrue(error_call._event.wait(1))
            with self.assertRaises(RuntimeError) as raised:
                error_call.result_until(time.monotonic() - 1)
            self.assertIs(raised.exception, error)

    def test_pending_timeout_records_state_before_returning_token(self):
        class IdleThread:
            ident = None

            def __init__(self, *_args, **_kwargs):
                pass

            def start(self):
                pass

            def join(self, _timeout=None):
                pass

        pool = network_calls.BoundedDaemonPool(1, "pending-token-timeout")
        with mock.patch.object(network_calls.threading, "Thread", IdleThread):
            pool.ensure_started()
            call = pool.submit(lambda: None)
            token = call._token
            try:
                with self.assertRaises(CallDeadlineExceeded):
                    call.result_until(time.monotonic() + 0.01)

                self.assertEqual(call._state.name, "ABANDONED_PENDING")
                self.assertEqual(call._abandonment.name, "DEADLINE")
                self.assertNotIn(call, pool._active)
                self.assertIn(token, pool._available_tokens)
                self.assertIsNone(pool._token_owners[token])
                self.assertEqual(_available_token_count(pool), 1)
            finally:
                pool.close_until(time.monotonic() + 0.2)

    def test_pending_state_is_recorded_before_token_becomes_available(self):
        class IdleThread:
            ident = None

            def __init__(self, *_args, **_kwargs):
                pass

            def start(self):
                pass

            def join(self, _timeout=None):
                pass

        pool = network_calls.BoundedDaemonPool(1, "pending-state-order")
        interruption = _CallerAbort("interrupted before token owner clear")
        observed_states = []
        with mock.patch.object(network_calls.threading, "Thread", IdleThread):
            call = pool.submit(lambda: None)

        def record_state(_frame):
            observed_states.append(call._state.name)

        try:
            with _raise_on_statement(
                network_calls.BoundedDaemonPool._return_token_locked,
                "self._token_owners[token] = None",
                interruption,
                before_raise=record_state,
            ):
                raised = _capture_error(
                    lambda: call.result_until(time.monotonic() + 0.01)
                )

            self.assertIs(raised, interruption)
            self.assertEqual(observed_states, ["ABANDONED_PENDING"])
            self.assertEqual(call._state.name, "ABANDONED_PENDING")
            self.assertEqual(call._abandonment.name, "DEADLINE")
            self.assertNotIn(call, pool._active)
            self.assertTrue(_token_is_returned(pool, call))
            self.assertEqual(_available_token_count(pool), 1)
        finally:
            pool.close_until(time.monotonic() + 0.2)

    def test_running_timeout_keeps_token_until_worker_finishes(self):
        pool = network_calls.BoundedDaemonPool(1, "running-token-timeout")
        started = threading.Event()
        release = threading.Event()

        def operation():
            started.set()
            if not release.wait(2):
                raise AssertionError("test did not release running token")

        call = pool.submit(operation)
        try:
            self.assertTrue(started.wait(1))
            with self.assertRaises(CallDeadlineExceeded):
                call.result_until(time.monotonic() + 0.01)

            self.assertEqual(call._state.name, "ABANDONED_RUNNING")
            self.assertIs(pool._token_owners[call._token], call)
            self.assertIn(call, pool._active)
            self.assertEqual(_available_token_count(pool), 0)
        finally:
            release.set()

        try:
            self.assertTrue(_wait_until(lambda: _token_is_returned(pool, call)))
            self.assertNotIn(call, pool._active)
            self.assertEqual(_available_token_count(pool), 1)
        finally:
            pool.close_until(time.monotonic() + 0.2)

    def test_unobservable_dequeue_abort_is_recovered_by_pending_timeout(self):
        pool = network_calls.BoundedDaemonPool(1, "dequeue-return-abort")
        dequeued = threading.Event()
        function_calls = []
        interruption = _WorkerAbort("interrupted before get assignment")
        original_get = pool._work_queue.get

        def abort_after_dequeue():
            original_get()
            dequeued.set()
            raise interruption

        try:
            with mock.patch.object(
                pool._work_queue,
                "get",
                side_effect=abort_after_dequeue,
            ), mock.patch("threading.excepthook"):
                call = pool.submit(lambda: function_calls.append(True))
                self.assertTrue(dequeued.wait(1))
                pool._workers[0].join(1)
                with self.assertRaises(CallDeadlineExceeded):
                    call.result_until(time.monotonic() + 0.01)

            self.assertEqual(call._state.name, "ABANDONED_PENDING")
            self.assertEqual(call._abandonment.name, "DEADLINE")
            self.assertEqual(function_calls, [])
            self.assertNotIn(call, pool._active)
            self.assertTrue(_token_is_returned(pool, call))
            self.assertEqual(_available_token_count(pool), 1)
        finally:
            pool.close_until(time.monotonic() + 0.2)
        pool.close_until(time.monotonic() + 0.2)

    def test_stale_queued_call_cannot_return_reassigned_token(self):
        pool = network_calls.BoundedDaemonPool(2, "stale-token")
        blocker_started = threading.Event()
        release_blocker = threading.Event()
        replacement_started = threading.Event()
        release_replacement = threading.Event()
        stale_calls = []

        def retire_worker():
            raise _WorkerAbort("retire one token worker")

        def block():
            blocker_started.set()
            if not release_blocker.wait(2):
                raise AssertionError("test did not release token blocker")
            return "blocker"

        def replacement_operation():
            replacement_started.set()
            if not release_replacement.wait(2):
                raise AssertionError("test did not release replacement")
            return "replacement"

        try:
            retired = pool.submit(retire_worker)
            with self.assertRaises(PoolUnavailableError):
                retired.result_until(time.monotonic() + 1)
            self.assertTrue(
                _wait_until(
                    lambda: sum(worker.is_alive() for worker in pool._workers)
                    == 1
                )
            )

            blocker = pool.submit(block)
            self.assertTrue(blocker_started.wait(1))
            stale = pool.submit(lambda: stale_calls.append(True))
            with self.assertRaises(CallDeadlineExceeded):
                stale.result_until(time.monotonic() + 0.01)
            self.assertEqual(stale._state.name, "ABANDONED_PENDING")
            self.assertEqual(_available_token_count(pool), 1)

            replacement = pool.submit(replacement_operation)
            self.assertIs(replacement._token, stale._token)
            self.assertIs(
                pool._token_owners[replacement._token],
                replacement,
            )
            release_blocker.set()
            self.assertTrue(replacement_started.wait(1))

            self.assertEqual(stale_calls, [])
            self.assertTrue(_token_is_returned(pool, stale))
            self.assertIs(
                pool._token_owners[replacement._token],
                replacement,
            )
            self.assertEqual(_available_token_count(pool), 1)

            release_replacement.set()
            self.assertEqual(
                blocker.result_until(time.monotonic() + 1),
                "blocker",
            )
            self.assertEqual(
                replacement.result_until(time.monotonic() + 1),
                "replacement",
            )
            self.assertEqual(_available_token_count(pool), 2)
        finally:
            release_blocker.set()
            release_replacement.set()
            pool.close_until(time.monotonic() + 0.2)

    def test_token_return_retries_repair_every_interruption_boundary(self):
        boundaries = (
            "self._active.discard(call)",
            "token = call._token",
            "self._available_tokens.add(token)",
            "self._token_owners[token] = None",
        )
        for statement in boundaries:
            with self.subTest(statement=statement):
                pool = network_calls.BoundedDaemonPool(1, "token-return-step")
                interruption = _CallerAbort(f"interrupted at {statement}")
                try:
                    with _raise_on_statement(
                        network_calls.BoundedDaemonPool._return_token_locked,
                        statement,
                        interruption,
                        new_threads=True,
                    ), mock.patch("threading.excepthook"):
                        call = pool.submit(lambda: "complete")
                        pool._workers[0].join(1)
                    self.assertEqual(
                        call.result_until(time.monotonic() + 1),
                        "complete",
                    )
                    self.assertFalse(pool._workers[0].is_alive())
                    self.assertNotIn(call, pool._active)
                    self.assertIsNone(pool._token_owners[call._token])
                    self.assertIn(call._token, pool._available_tokens)
                    self.assertEqual(_available_token_count(pool), 1)
                finally:
                    pool.close_until(time.monotonic() + 0.2)

        pool = network_calls.BoundedDaemonPool(1, "token-return-complete")
        interruption = _CallerAbort("interrupted after token return")
        try:
            with _raise_on_return(
                network_calls.BoundedDaemonPool._return_token_locked,
                interruption,
                new_threads=True,
            ), mock.patch("threading.excepthook"):
                call = pool.submit(lambda: "complete")
                pool._workers[0].join(1)
            self.assertEqual(
                call.result_until(time.monotonic() + 1),
                "complete",
            )
            self.assertFalse(pool._workers[0].is_alive())
            self.assertNotIn(call, pool._active)
            self.assertIsNone(pool._token_owners[call._token])
            self.assertEqual(_available_token_count(pool), 1)
        finally:
            pool.close_until(time.monotonic() + 0.2)

    def test_token_return_interruption_exposes_only_ordered_partial_states(self):
        probes = (
            (
                "if self._token_owners[token] is call:",
                (True, False, True),
            ),
            (
                "self._available_tokens.add(token)",
                (True, False, True),
            ),
            (
                "self._token_owners[token] = None",
                (True, True, True),
            ),
            (
                "self._active.discard(call)",
                (False, True, True),
            ),
        )
        for statement, expected in probes:
            with self.subTest(statement=statement):
                pool = network_calls.BoundedDaemonPool(1, "token-order")
                pool._startup_attempted = True
                pool._startup_succeeded = True
                call = pool.submit(lambda: None)
                pool._work_queue.get_nowait()
                interruption = _CallerAbort(f"interrupted at {statement}")
                try:
                    with _raise_on_statement(
                        network_calls.BoundedDaemonPool._return_token_locked,
                        statement,
                        interruption,
                    ):
                        with pool._state_lock:
                            raised = _capture_error(
                                lambda: pool._return_token_locked(call)
                            )

                    self.assertIs(raised, interruption)
                    owner_is_call = pool._token_owners[call._token] is call
                    token_is_available = (
                        call._token in pool._available_tokens
                    )
                    call_is_active = call in pool._active
                    self.assertEqual(
                        (owner_is_call, token_is_available, call_is_active),
                        expected,
                    )

                    with pool._state_lock:
                        pool._return_token_locked(call)
                    self.assertTrue(_token_is_returned(pool, call))
                    self.assertEqual(_available_token_count(pool), 1)
                    self.assertNotIn(call, pool._active)
                finally:
                    pool.begin_close()

    def test_interrupted_return_cannot_reclaim_reassigned_token(self):
        class IdleThread:
            ident = None

            def __init__(self, *_args, **_kwargs):
                pass

            def start(self):
                pass

            def join(self, _timeout=None):
                pass

        pool = network_calls.BoundedDaemonPool(1, "reassigned-token")
        interruption = _CallerAbort("interrupted before owner clear")
        with mock.patch.object(network_calls.threading, "Thread", IdleThread):
            stale = pool.submit(lambda: None)
            pool._work_queue.get_nowait()

            def return_stale_once():
                with pool._state_lock:
                    pool._return_token_locked(stale)

            try:
                with _raise_on_statement(
                    network_calls.BoundedDaemonPool._return_token_locked,
                    "self._token_owners[token] = None",
                    interruption,
                ):
                    raised = _capture_error(return_stale_once)
                self.assertIs(raised, interruption)
                self.assertIn(stale._token, pool._available_tokens)
                self.assertIs(pool._token_owners[stale._token], stale)
                self.assertIn(stale, pool._active)

                replacement = pool.submit(lambda: None)
                self.assertIs(replacement._token, stale._token)
                self.assertIs(
                    pool._token_owners[replacement._token],
                    replacement,
                )
                with pool._state_lock:
                    pool._return_token_locked(stale)
                self.assertEqual(_available_token_count(pool), 0)
                self.assertIs(
                    pool._token_owners[replacement._token],
                    replacement,
                )
                self.assertIn(replacement, pool._active)
            finally:
                pool.close_until(time.monotonic() + 0.2)

    def test_worker_death_repairs_each_partial_abandonment_step(self):
        probes = (
            (
                network_calls.BoundedDaemonPool._abandon_locked,
                "call._abandonment = reason",
            ),
            (
                network_calls.BoundedDaemonPool._abandon_locked,
                "self._signal_locked(call)",
            ),
            (
                network_calls.BoundedDaemonPool._signal_locked,
                "with self._completion_condition:",
            ),
        )
        for function, statement in probes:
            with self.subTest(statement=statement):
                pool = network_calls.BoundedDaemonPool(1, "abandon-repair")
                interruption = _WorkerAbort(f"interrupted at {statement}")

                def abort_operation():
                    raise _WorkerAbort("operation stopped worker")

                try:
                    with _raise_on_statement(
                        function,
                        statement,
                        interruption,
                        new_threads=True,
                    ), mock.patch("threading.excepthook"):
                        call = pool.submit(abort_operation)
                        pool._workers[0].join(1)

                    first_error = _capture_error(
                        lambda: call.result_until(time.monotonic() + 0.05)
                    )
                    second_error = _capture_error(
                        lambda: call.result_until(time.monotonic() + 0.05)
                    )
                    self.assertIs(type(first_error), PoolUnavailableError)
                    self.assertIs(type(second_error), PoolUnavailableError)
                    self.assertIsNot(first_error, second_error)
                    self.assertEqual(call._state.name, "ABANDONED_RUNNING")
                    self.assertIs(
                        call._abandonment,
                        network_calls._AbandonmentReason.WORKER_DIED,
                    )
                    self.assertTrue(call._event.is_set())
                    self.assertGreater(pool._completion_generation, 0)
                    self.assertNotIn(call, pool._active)
                    self.assertTrue(_token_is_returned(pool, call))
                    self.assertEqual(_available_token_count(pool), 1)
                finally:
                    pool.close_until(time.monotonic() + 0.2)

    def test_worker_death_discards_outcome_from_partial_running_finish(self):
        pool = network_calls.BoundedDaemonPool(1, "partial-running-finish")
        interruption = _WorkerAbort("interrupted before completed state")
        value = object()
        try:
            with _raise_on_statement(
                network_calls.BoundedDaemonPool._finish,
                "call._state = _CallState.COMPLETED",
                interruption,
                new_threads=True,
            ), mock.patch("threading.excepthook"):
                call = pool.submit(lambda: value)
                pool._workers[0].join(1)

            error = _capture_error(
                lambda: call.result_until(time.monotonic() + 0.05)
            )
            self.assertIs(type(error), PoolUnavailableError)
            self.assertEqual(call._state.name, "ABANDONED_RUNNING")
            self.assertEqual(call._abandonment.name, "WORKER_DIED")
            self.assertIsNone(call._outcome)
            self.assertTrue(call._event.is_set())
            self.assertNotIn(call, pool._active)
            self.assertTrue(_token_is_returned(pool, call))
            self.assertEqual(_available_token_count(pool), 1)
        finally:
            pool.close_until(time.monotonic() + 0.2)

    def test_worker_death_repairs_partial_completed_outcome_exactly(self):
        for outcome_kind in ("value", "error"):
            with self.subTest(outcome_kind=outcome_kind):
                pool = network_calls.BoundedDaemonPool(
                    1,
                    "partial-completed-finish",
                )
                interruption = _WorkerAbort(
                    "interrupted before completion signal"
                )
                value = object()
                operation_error = RuntimeError("exact completed failure")

                def operation():
                    if outcome_kind == "error":
                        raise operation_error
                    return value

                try:
                    with _raise_on_statement(
                        network_calls.BoundedDaemonPool._finish,
                        "self._signal_locked(call)",
                        interruption,
                        new_threads=True,
                    ), mock.patch("threading.excepthook"):
                        call = pool.submit(operation)
                        pool._workers[0].join(1)

                    self.assertEqual(call._state.name, "COMPLETED")
                    self.assertTrue(call._event.is_set())
                    self.assertGreater(pool._completion_generation, 0)
                    self.assertNotIn(call, pool._active)
                    self.assertTrue(_token_is_returned(pool, call))
                    if outcome_kind == "error":
                        raised = _capture_error(
                            lambda: call.result_until(time.monotonic() + 1)
                        )
                        self.assertIs(raised, operation_error)
                    else:
                        self.assertIs(
                            call.result_until(time.monotonic() + 1),
                            value,
                        )
                finally:
                    pool.close_until(time.monotonic() + 0.2)

    def test_finish_retry_repairs_partial_completed_publication(self):
        pool = network_calls.BoundedDaemonPool(1, "finish-retry")
        pool._startup_attempted = True
        pool._startup_succeeded = True
        call = pool.submit(lambda: None)
        pool._work_queue.get_nowait()
        call._state = network_calls._CallState.RUNNING
        value = object()
        outcome = CallOutcome.success(value)
        interruption = _WorkerAbort("completion signal interrupted")
        try:
            with _raise_on_statement(
                network_calls.BoundedDaemonPool._finish,
                "self._signal_locked(call)",
                interruption,
            ):
                raised = _capture_error(lambda: pool._finish(call, outcome))

            self.assertIs(raised, interruption)
            self.assertEqual(call._state.name, "COMPLETED")
            self.assertIs(call._outcome, outcome)
            self.assertFalse(call._event.is_set())
            self.assertIs(pool._token_owners[call._token], call)

            pool._finish(call, outcome)

            self.assertTrue(call._event.is_set())
            self.assertGreater(pool._completion_generation, 0)
            self.assertNotIn(call, pool._active)
            self.assertTrue(_token_is_returned(pool, call))
            self.assertIs(
                call.result_until(time.monotonic() + 1),
                value,
            )
        finally:
            pool.begin_close()

    def test_repeated_abandonment_preserves_reason_and_resignals(self):
        class IdleThread:
            ident = None

            def __init__(self, *_args, **_kwargs):
                pass

            def start(self):
                pass

            def join(self, _timeout=None):
                pass

        class CountingEvent:
            def __init__(self, event):
                self._event = event
                self.set_count = 0

            def set(self):
                self.set_count += 1
                self._event.set()

            def wait(self, timeout=None):
                return self._event.wait(timeout)

            def is_set(self):
                return self._event.is_set()

        pool = network_calls.BoundedDaemonPool(1, "repeated-abandon")
        with mock.patch.object(network_calls.threading, "Thread", IdleThread):
            call = pool.submit(lambda: None)
        call._event = CountingEvent(call._event)
        initial_generation = pool._completion_generation
        try:
            with pool._state_lock:
                with call._lock:
                    pool._abandon_locked(
                        call,
                        network_calls._AbandonmentReason.DEADLINE,
                    )
                    pool._abandon_locked(
                        call,
                        network_calls._AbandonmentReason.CLOSED,
                    )

            self.assertEqual(call._state.name, "ABANDONED_PENDING")
            self.assertIs(
                call._abandonment,
                network_calls._AbandonmentReason.DEADLINE,
            )
            self.assertEqual(call._event.set_count, 2)
            self.assertEqual(
                pool._completion_generation,
                initial_generation + 2,
            )
            self.assertNotIn(call, pool._active)
            self.assertTrue(_token_is_returned(pool, call))
        finally:
            pool.close_until(time.monotonic() + 0.2)

    def test_timeout_abandons_running_calls_and_discards_late_outcomes(self):
        for outcome_kind in ("value", "error"):
            with self.subTest(outcome_kind=outcome_kind):
                with installed_network_pools() as pools:
                    started = threading.Event()
                    release = threading.Event()
                    late_reference = []

                    def operation():
                        started.set()
                        if not release.wait(2):
                            raise AssertionError("test did not release operation")
                        if outcome_kind == "error":
                            raise RuntimeError("late operation error")
                        value = _LargeValue()
                        late_reference.append(weakref.ref(value))
                        return value

                    call = pools.dns.submit(operation)
                    try:
                        self.assertTrue(started.wait(1))
                        with self.assertRaises(CallDeadlineExceeded):
                            call.result_until(time.monotonic() + 0.02)
                        self.assertEqual(
                            call._state.name,
                            "ABANDONED_RUNNING",
                        )
                        self.assertEqual(call._abandonment.name, "DEADLINE")
                        self.assertIsNone(call._outcome)
                    finally:
                        release.set()

                    self.assertTrue(
                        _wait_until(
                            lambda: _token_is_returned(pools.dns, call),
                            timeout=0.2,
                        )
                    )
                    self.assertNotIn(call, pools.dns._active)
                    self.assertIsNone(call._outcome)
                    self.assertEqual(_available_token_count(pools.dns), 4)
                    if late_reference:
                        gc.collect()
                        self.assertIsNone(late_reference[0]())

    def test_queued_abandoned_call_never_invokes_its_function(self):
        class IdleThread:
            ident = None

            def __init__(self, *_args, **_kwargs):
                pass

            def start(self):
                pass

            def join(self, _timeout=None):
                pass

        pool = network_calls.BoundedDaemonPool(1, "idle-worker")
        invoked = []
        with mock.patch.object(network_calls.threading, "Thread", IdleThread):
            pool.ensure_started()
            call = pool.submit(lambda: invoked.append(True))
            with self.assertRaises(CallDeadlineExceeded):
                call.result_until(time.monotonic() + 0.01)
            pool.begin_close()
            pool.join_until(time.monotonic() + 0.01)

        self.assertEqual(invoked, [])
        self.assertEqual(call._state.name, "ABANDONED_PENDING")
        self.assertIsNone(call._outcome)
        self.assertEqual(len(pool._active), 0)
        self.assertTrue(pool._work_queue.empty())
        self.assertEqual(_available_token_count(pool), 1)

    def test_real_worker_skips_timed_out_call_queued_behind_blocker(self):
        pool = network_calls.BoundedDaemonPool(2, "queued-skip")
        first_started = threading.Event()
        release_first = threading.Event()
        second_calls = []
        self.addCleanup(
            lambda: pool.close_until(time.monotonic() + 0.2)
        )
        self.addCleanup(release_first.set)

        def retire_worker():
            raise _WorkerAbort("retire one worker")

        def block_first():
            first_started.set()
            if not release_first.wait(2):
                raise AssertionError("test did not release first queued call")

        try:
            retired = pool.submit(retire_worker)
            with self.assertRaises(PoolUnavailableError):
                retired.result_until(time.monotonic() + 1)
            self.assertTrue(
                _wait_until(
                    lambda: sum(
                        worker.is_alive() for worker in pool._workers
                    )
                    == 1
                )
            )

            first = pool.submit(block_first)
            self.assertTrue(first_started.wait(1))
            second = pool.submit(lambda: second_calls.append(True))
            with self.assertRaises(CallDeadlineExceeded):
                second.result_until(time.monotonic() + 0.02)
        finally:
            release_first.set()

        try:
            self.assertTrue(
                _wait_until(lambda: _token_is_returned(pool, second), timeout=0.2)
            )
            first.result_until(time.monotonic() + 1)
            self.assertEqual(second_calls, [])
            self.assertIsNone(second._outcome)
            self.assertEqual(len(pool._active), 0)
            self.assertEqual(_available_token_count(pool), 2)
        finally:
            pool.close_until(time.monotonic() + 0.2)

    def test_worker_base_exception_wakes_caller_and_retires_worker(self):
        pool = network_calls.BoundedDaemonPool(1, "fatal-worker")
        started = threading.Event()

        def abort():
            started.set()
            raise _WorkerAbort("worker stopped")

        try:
            call = pool.submit(abort)
            self.assertTrue(started.wait(1))
            first_error = _capture_error(
                lambda: call.result_until(time.monotonic() + 1)
            )
            second_error = _capture_error(
                lambda: call.result_until(time.monotonic() + 1)
            )
            for worker in pool._workers:
                worker.join(1)

            self.assertIs(type(first_error), PoolUnavailableError)
            self.assertIs(type(second_error), PoolUnavailableError)
            self.assertIsNot(first_error, second_error)
            self.assertEqual(call._abandonment.name, "WORKER_DIED")
            self.assertIsNone(call._outcome)
            self.assertEqual(len(pool._active), 0)
            self.assertEqual(_available_token_count(pool), 1)
            self.assertEqual(len(pool._workers), 1)
            self.assertFalse(pool._workers[0].is_alive())

            replacement = pool.submit(lambda: "must not run")
            with self.assertRaises(CallDeadlineExceeded):
                replacement.result_until(time.monotonic() + 0.01)
            self.assertEqual(len(pool._workers), 1)
        finally:
            pool.close_until(time.monotonic() + 0.2)

    def test_close_pending_claim_repairs_signals_and_returns_token_once(self):
        class IdleThread:
            ident = None

            def __init__(self, *_args, **_kwargs):
                pass

            def start(self):
                pass

            def join(self, _timeout=None):
                pass

        class CountingEvent:
            def __init__(self, event):
                self._event = event
                self.set_count = 0

            def set(self):
                self.set_count += 1
                self._event.set()

            def wait(self, timeout=None):
                return self._event.wait(timeout)

            def is_set(self):
                return self._event.is_set()

        pool = network_calls.BoundedDaemonPool(1, "idle-worker")
        with mock.patch.object(network_calls.threading, "Thread", IdleThread):
            pool.ensure_started()
            call = pool.submit(lambda: None)
            call._event = CountingEvent(call._event)
            pool.begin_close()

        first_error = _capture_error(
            lambda: call.result_until(time.monotonic() + 1)
        )
        second_error = _capture_error(
            lambda: call.result_until(time.monotonic() + 1)
        )
        self.assertIs(type(first_error), PoolUnavailableError)
        self.assertIs(type(second_error), PoolUnavailableError)
        self.assertIsNot(first_error, second_error)
        self.assertEqual(call._event.set_count, 4)
        self.assertEqual(_available_token_count(pool), 1)
        self.assertEqual(len(pool._active), 0)

    def test_close_wins_after_worker_dequeues_but_before_claim(self):
        pool = network_calls.BoundedDaemonPool(1, "dequeued-close")
        claim_entered = threading.Event()
        allow_claim = threading.Event()
        close_finished = threading.Event()
        function_calls = []
        original_claim = pool._claim
        self.addCleanup(
            lambda: pool.close_until(time.monotonic() + 0.2)
        )
        self.addCleanup(allow_claim.set)

        def delayed_claim(call):
            claim_entered.set()
            if not allow_claim.wait(2):
                raise AssertionError("test did not release delayed claim")
            return original_claim(call)

        def close_pool():
            pool.begin_close()
            close_finished.set()

        with mock.patch.object(pool, "_claim", side_effect=delayed_claim):
            call = pool.submit(lambda: function_calls.append(True))
            closer = threading.Thread(target=close_pool)
            closer_started = False
            try:
                self.assertTrue(claim_entered.wait(1))
                closer.start()
                closer_started = True
                self.assertTrue(close_finished.wait(1))
                error = _capture_error(
                    lambda: call.result_until(time.monotonic() + 1)
                )
                self.assertEqual(len(pool._active), 0)
                self.assertEqual(_available_token_count(pool), 1)
            finally:
                allow_claim.set()
                if closer_started:
                    closer.join(1)
                pool.close_until(time.monotonic() + 0.2)

        self.assertFalse(closer.is_alive())
        self.assertIs(type(error), PoolUnavailableError)
        self.assertEqual(function_calls, [])
        self.assertTrue(_token_is_returned(pool, call))
        self.assertEqual(len(pool._active), 0)
        self.assertEqual(_available_token_count(pool), 1)

    def test_close_versus_completion_publishes_only_one_terminal_state(self):
        pool = network_calls.BoundedDaemonPool(1, "completion-race")
        started = threading.Event()
        release = threading.Event()
        caller_finished = threading.Event()
        caller_outcomes = []
        value = object()

        def operation():
            started.set()
            if not release.wait(2):
                raise AssertionError("test did not release operation")
            return value

        call = pool.submit(operation)

        def await_result():
            caller_outcomes.append(
                _capture_value_or_error(
                    lambda: call.result_until(time.monotonic() + 1)
                )
            )
            caller_finished.set()

        caller = threading.Thread(target=await_result)
        caller.start()
        try:
            self.assertTrue(started.wait(1))
            release.set()
            pool.begin_close()
            self.assertTrue(caller_finished.wait(1))
        finally:
            release.set()
            caller.join(1)
            pool.close_until(time.monotonic() + 0.2)
        self.assertFalse(caller.is_alive())
        self.assertEqual(len(caller_outcomes), 1)
        succeeded, outcome = caller_outcomes[0]
        if succeeded:
            self.assertIs(outcome, value)
        else:
            self.assertIs(type(outcome), PoolUnavailableError)
        self.assertEqual(_available_token_count(pool), 1)
        self.assertEqual(len(pool._active), 0)

    def test_close_versus_timeout_keeps_first_abandonment_and_one_release(self):
        pool = network_calls.BoundedDaemonPool(1, "timeout-race")
        started = threading.Event()
        release = threading.Event()
        caller_finished = threading.Event()
        caller_errors = []

        def operation():
            started.set()
            if not release.wait(2):
                raise AssertionError("test did not release operation")

        call = pool.submit(operation)

        def await_result():
            caller_errors.append(
                _capture_error(
                    lambda: call.result_until(time.monotonic() + 0.02)
                )
            )
            caller_finished.set()

        caller = threading.Thread(target=await_result)
        caller.start()
        try:
            self.assertTrue(started.wait(1))
            pool.begin_close()
            self.assertTrue(caller_finished.wait(1))
        finally:
            release.set()
            caller.join(1)
            pool.close_until(time.monotonic() + 0.2)
        self.assertFalse(caller.is_alive())
        self.assertEqual(len(caller_errors), 1)
        self.assertIsInstance(
            caller_errors[0],
            (CallDeadlineExceeded, PoolUnavailableError),
        )
        self.assertIn(call._abandonment.name, {"DEADLINE", "CLOSED"})
        self.assertEqual(_available_token_count(pool), 1)
        self.assertEqual(len(pool._active), 0)

    def test_caller_worker_and_closer_cannot_return_token_twice(self):
        pool = network_calls.BoundedDaemonPool(1, "cleanup-race")
        started = threading.Event()
        release = threading.Event()
        caller_finished = threading.Event()
        failures = []

        def operation():
            started.set()
            if not release.wait(2):
                raise AssertionError("test did not release operation")
            return "late"

        call = pool.submit(operation)

        def await_result():
            try:
                call.result_until(time.monotonic() + 0.01)
            except (CallDeadlineExceeded, PoolUnavailableError):
                pass
            except BaseException as error:
                failures.append(error)
            finally:
                caller_finished.set()

        caller = threading.Thread(target=await_result)
        caller.start()
        try:
            self.assertTrue(started.wait(1))
            closer = threading.Thread(target=pool.begin_close)
            closer.start()
            self.assertTrue(caller_finished.wait(1))
            release.set()
            closer.join(1)
            self.assertFalse(closer.is_alive())
        finally:
            release.set()
            caller.join(1)
            pool.close_until(time.monotonic() + 0.2)

        self.assertFalse(caller.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(_token_is_returned(pool, call))
        with pool._state_lock:
            with call._lock:
                pool._return_token_locked(call)
                pool._return_token_locked(call)
        self.assertEqual(_available_token_count(pool), 1)
        self.assertEqual(len(pool._active), 0)

    def test_pool_cleanup_obeys_state_then_call_lock_order(self):
        class IdleThread:
            ident = None

            def __init__(self, *_args, **_kwargs):
                pass

            def start(self):
                pass

            def join(self, _timeout=None):
                pass

        class RecordingLock:
            def __init__(self, lock):
                self._lock = lock
                self.owner = None

            def __enter__(self):
                self._lock.acquire()
                self.owner = threading.get_ident()
                return self

            def __exit__(self, *_args):
                self.owner = None
                self._lock.release()

        class OrderedCallLock:
            def __init__(self, lock, state_lock):
                self._lock = lock
                self._state_lock = state_lock

            def __enter__(self):
                if self._state_lock.owner != threading.get_ident():
                    raise AssertionError("call lock acquired before pool state lock")
                self._lock.acquire()
                return self

            def __exit__(self, *_args):
                self._lock.release()

        pool = network_calls.BoundedDaemonPool(1, "lock-order")
        with mock.patch.object(network_calls.threading, "Thread", IdleThread):
            pool.ensure_started()
            call = pool.submit(lambda: None)
            state_lock = RecordingLock(pool._state_lock)
            call_lock = OrderedCallLock(call._lock, state_lock)
            pool._state_lock = state_lock
            call._lock = call_lock
            with self.assertRaises(CallDeadlineExceeded):
                call.result_until(time.monotonic() - 1)
            pool.begin_close()

        self.assertEqual(_available_token_count(pool), 1)
        self.assertEqual(len(pool._active), 0)

    def test_caller_base_exception_abandons_and_reraises_by_identity(self):
        class InterruptingEvent:
            def __init__(self, event, interruption):
                self._event = event
                self._interruption = interruption

            def set(self):
                self._event.set()

            def wait(self, _timeout=None):
                raise self._interruption

            def is_set(self):
                return self._event.is_set()

        pool = network_calls.BoundedDaemonPool(1, "caller-interrupt")
        started = threading.Event()
        release = threading.Event()
        late_reference = []
        interruption = _CallerAbort("caller interrupted")

        def operation():
            started.set()
            if not release.wait(2):
                raise AssertionError("test did not release operation")
            value = _LargeValue()
            late_reference.append(weakref.ref(value))
            return value

        call = pool.submit(operation)
        try:
            self.assertTrue(started.wait(1))
            call._event = InterruptingEvent(call._event, interruption)
            raised = _capture_error(
                lambda: call.result_until(time.monotonic() + 1)
            )
            self.assertIs(raised, interruption)
            self.assertEqual(call._state.name, "ABANDONED_RUNNING")
            self.assertEqual(call._abandonment.name, "INTERRUPTED")
            self.assertIsNone(call._outcome)
        finally:
            release.set()
            pool.close_until(time.monotonic() + 0.2)

        self.assertTrue(_wait_until(lambda: not pool._active))
        gc.collect()
        self.assertEqual(len(late_reference), 1)
        self.assertIsNone(late_reference[0]())
        self.assertEqual(_available_token_count(pool), 1)

    def test_caller_interruption_before_completed_consume_clears_value(self):
        class ObservedWaitEvent:
            def __init__(self, event):
                self._event = event
                self.waited = False

            def set(self):
                self._event.set()

            def wait(self, timeout=None):
                result = self._event.wait(timeout)
                self.waited = True
                return result

            def is_set(self):
                return self._event.is_set()

        pool = network_calls.BoundedDaemonPool(1, "published-interrupt")
        probe_entered = threading.Event()
        release_probe = threading.Event()
        value_reference = []
        interruption = _CallerAbort("interrupted before consume")

        def operation():
            value = _LargeValue()
            value_reference.append(weakref.ref(value))
            return value

        try:
            call = pool.submit(operation)
            self.assertTrue(call._event.wait(1))
            observed_wait = ObservedWaitEvent(call._event)
            call._event = observed_wait

            def completed_before_arbitration(frame):
                return (
                    frame.f_code is BoundedCall.result_until.__code__
                    and observed_wait.waited
                    and frame.f_locals.get("outcome") is None
                    and call._state is network_calls._CallState.COMPLETED
                )

            with _raise_when(completed_before_arbitration, interruption):
                raised = _capture_error(
                    lambda: call.result_until(time.monotonic() + 1)
                )

            self.assertIs(raised, interruption)
            self.assertEqual(call._state.name, "CONSUMED")
            self.assertIsNone(call._outcome)

            def probe_operation():
                probe_entered.set()
                if not release_probe.wait(2):
                    raise AssertionError("test did not release consume probe")

            pool.submit(probe_operation)
            self.assertTrue(probe_entered.wait(1))
            gc.collect()
            self.assertEqual(len(value_reference), 1)
            self.assertIsNone(value_reference[0]())
        finally:
            release_probe.set()
            pool.close_until(time.monotonic() + 0.2)

    def test_result_until_interruption_after_completed_detach_clears_traceback_value(self):
        pool = network_calls.BoundedDaemonPool(1, "consume-detach-interrupt")
        probe_entered = threading.Event()
        release_probe = threading.Event()
        value_reference = []
        interrupted_codes = []
        interruption = _CallerAbort("interrupted after completed detach")

        def operation():
            value = _LargeValue()
            value_reference.append(weakref.ref(value))
            return value

        try:
            call = pool.submit(operation)
            self.assertTrue(call._event.wait(1))
            with _interrupt_after_outcome_detach(
                call,
                interruption,
                interrupted_codes,
            ):
                raised = _capture_error(
                    lambda: call.result_until(time.monotonic() + 1)
                )

            self.assertIs(raised, interruption)
            self.assertEqual(
                interrupted_codes,
                [BoundedCall.result_until.__code__],
            )
            self.assertEqual(call._state.name, "CONSUMED")
            self.assertIsNone(call._outcome)

            def probe_operation():
                probe_entered.set()
                if not release_probe.wait(2):
                    raise AssertionError("test did not release consume probe")

            pool.submit(probe_operation)
            self.assertTrue(probe_entered.wait(1))
            gc.collect()
            self.assertEqual(len(value_reference), 1)
            self.assertIsNone(value_reference[0]())
        finally:
            release_probe.set()
            pool.close_until(time.monotonic() + 0.2)

    def test_caller_interruption_during_outcome_extraction_clears_outcome(self):
        pool = network_calls.BoundedDaemonPool(1, "extract-interrupt")
        probe_entered = threading.Event()
        release_probe = threading.Event()
        value_reference = []
        interruption = _CallerAbort("interrupted during outcome extraction")

        def operation():
            value = _LargeValue()
            value_reference.append(weakref.ref(value))
            return value

        try:
            call = pool.submit(operation)
            self.assertTrue(call._event.wait(1))
            with _raise_on_statement(
                BoundedCall.result_until,
                "value = outcome.value",
                interruption,
            ):
                raised = _capture_error(
                    lambda: call.result_until(time.monotonic() + 1)
                )

            self.assertIs(raised, interruption)
            self.assertEqual(call._state.name, "CONSUMED")
            self.assertIsNone(call._outcome)

            def probe_operation():
                probe_entered.set()
                if not release_probe.wait(2):
                    raise AssertionError("test did not release extract probe")

            pool.submit(probe_operation)
            self.assertTrue(probe_entered.wait(1))
            gc.collect()
            self.assertEqual(len(value_reference), 1)
            self.assertIsNone(value_reference[0]())
        finally:
            release_probe.set()
            pool.close_until(time.monotonic() + 0.2)

    def test_caller_interruption_before_timeout_transition_discards_late_value(self):
        class FalseWaitEvent:
            def __init__(self, event):
                self._event = event
                self.waited = False

            def set(self):
                self._event.set()

            def wait(self, _timeout=None):
                self.waited = True
                return False

            def is_set(self):
                return self._event.is_set()

        pool = network_calls.BoundedDaemonPool(1, "timeout-interrupt")
        started = threading.Event()
        release = threading.Event()
        probe_entered = threading.Event()
        release_probe = threading.Event()
        value_reference = []
        interruption = _CallerAbort("interrupted before timeout transition")

        def operation():
            started.set()
            if not release.wait(2):
                raise AssertionError("test did not release interrupted call")
            value = _LargeValue()
            value_reference.append(weakref.ref(value))
            return value

        try:
            call = pool.submit(operation)
            self.assertTrue(started.wait(1))
            false_wait = FalseWaitEvent(call._event)
            call._event = false_wait

            def running_after_false_wait(frame):
                return (
                    frame.f_code is BoundedCall.result_until.__code__
                    and false_wait.waited
                    and frame.f_locals.get("outcome") is None
                    and call._state is network_calls._CallState.RUNNING
                )

            with _raise_when(running_after_false_wait, interruption):
                raised = _capture_error(
                    lambda: call.result_until(time.monotonic() + 1)
                )
            self.assertIs(raised, interruption)
            self.assertEqual(call._state.name, "ABANDONED_RUNNING")
            self.assertEqual(call._abandonment.name, "INTERRUPTED")
            self.assertIsNone(call._outcome)
        finally:
            release.set()

        try:
            self.assertTrue(
                _wait_until(
                    lambda: _token_is_returned(pool, call),
                    timeout=0.2,
                )
            )

            def probe_operation():
                probe_entered.set()
                if not release_probe.wait(2):
                    raise AssertionError("test did not release timeout probe")

            pool.submit(probe_operation)
            self.assertTrue(probe_entered.wait(1))
            gc.collect()
            self.assertEqual(len(value_reference), 1)
            self.assertIsNone(value_reference[0]())
        finally:
            release_probe.set()
            pool.close_until(time.monotonic() + 0.2)

    def test_result_until_false_wait_completion_detach_clears_traceback_value(self):
        class FalseWaitEvent:
            def __init__(self, event):
                self._event = event

            def set(self):
                self._event.set()

            def wait(self, _timeout=None):
                return False

            def is_set(self):
                return self._event.is_set()

        pool = network_calls.BoundedDaemonPool(1, "timeout-detach-interrupt")
        probe_entered = threading.Event()
        release_probe = threading.Event()
        value_reference = []
        interrupted_codes = []
        interruption = _CallerAbort("interrupted after timeout detach")

        def operation():
            value = _LargeValue()
            value_reference.append(weakref.ref(value))
            return value

        try:
            call = pool.submit(operation)
            self.assertTrue(call._event.wait(1))
            call._event = FalseWaitEvent(call._event)
            with _interrupt_after_outcome_detach(
                call,
                interruption,
                interrupted_codes,
            ):
                raised = _capture_error(
                    lambda: call.result_until(time.monotonic() + 1)
                )

            self.assertIs(raised, interruption)
            self.assertEqual(
                interrupted_codes,
                [BoundedCall.result_until.__code__],
            )
            self.assertEqual(call._state.name, "CONSUMED")
            self.assertIsNone(call._outcome)

            def probe_operation():
                probe_entered.set()
                if not release_probe.wait(2):
                    raise AssertionError("test did not release timeout probe")

            pool.submit(probe_operation)
            self.assertTrue(probe_entered.wait(1))
            gc.collect()
            self.assertEqual(len(value_reference), 1)
            self.assertIsNone(value_reference[0]())
        finally:
            release_probe.set()
            pool.close_until(time.monotonic() + 0.2)

    def test_pool_success_does_not_enter_public_outcome_result_frame(self):
        pool = network_calls.BoundedDaemonPool(1, "direct-outcome-success")
        probe_entered = threading.Event()
        release_probe = threading.Event()
        value_reference = []
        errors = []
        interruption = _CallerAbort("internal outcome.result entered")

        def operation():
            value = _LargeValue()
            value_reference.append(weakref.ref(value))
            return value

        try:
            call = pool.submit(operation)
            self.assertTrue(call._event.wait(1))
            with _raise_on_statement(
                CallOutcome.result,
                "if self.succeeded:",
                interruption,
            ):
                try:
                    call.result_until(time.monotonic() + 1)
                except BaseException as error:
                    errors.append(error)

            self.assertEqual(errors, [])
            self.assertEqual(call._state.name, "CONSUMED")
            self.assertIsNone(call._outcome)

            def probe_operation():
                probe_entered.set()
                if not release_probe.wait(2):
                    raise AssertionError("test did not release outcome probe")

            pool.submit(probe_operation)
            self.assertTrue(probe_entered.wait(1))
            gc.collect()
            self.assertEqual(len(value_reference), 1)
            self.assertIsNone(value_reference[0]())
        finally:
            release_probe.set()
            pool.close_until(time.monotonic() + 0.2)

    def test_map_interruption_clears_completed_and_abandons_running_calls(self):
        pool = network_calls.BoundedDaemonPool(2, "map-interrupt")
        allow_completion = threading.Event()
        blocked_started = threading.Event()
        release_blocked = threading.Event()
        completed_reference = []
        interruption = _CallerAbort("batch interrupted")
        original_wait = pool._completion_condition.wait
        wait_count = 0

        def operation(item):
            if item == "complete":
                if not allow_completion.wait(2):
                    raise AssertionError("test did not permit completion")
                value = _LargeValue()
                completed_reference.append(weakref.ref(value))
                return value
            blocked_started.set()
            if not release_blocked.wait(2):
                raise AssertionError("test did not release blocked call")
            return _LargeValue()

        def interrupt_second_wait(timeout=None):
            nonlocal wait_count
            wait_count += 1
            if wait_count == 1:
                allow_completion.set()
                return original_wait(timeout)
            raise interruption

        pool._completion_condition.wait = interrupt_second_wait
        try:
            raised = _capture_error(
                lambda: pool.map_until(
                    time.monotonic() + 1,
                    operation,
                    ("complete", "blocked"),
                )
            )
            self.assertIs(raised, interruption)
            self.assertTrue(blocked_started.is_set())
            self.assertEqual(len(completed_reference), 1)
            gc.collect()
            self.assertIsNone(completed_reference[0]())
            self.assertEqual(len(pool._active), 1)
            active_call = next(iter(pool._active))
            self.assertEqual(
                active_call._state.name,
                "ABANDONED_RUNNING",
            )
            self.assertEqual(active_call._abandonment.name, "INTERRUPTED")
            self.assertIsNone(active_call._outcome)
        finally:
            release_blocked.set()
            pool.close_until(time.monotonic() + 0.2)

        self.assertTrue(_wait_until(lambda: not pool._active))
        self.assertEqual(_available_token_count(pool), 2)

    def test_map_wrap_interruption_clears_completed_value_from_traceback(self):
        pool = network_calls.BoundedDaemonPool(2, "map-wrap-interrupt")
        blocked_started = threading.Event()
        release_blocked = threading.Event()
        probe_entered = threading.Event()
        value_reference = []
        interruption = _CallerAbort("interrupted while wrapping map value")
        caller_thread = threading.get_ident()
        real_success = CallOutcome.success

        def operation(item):
            if item == "complete":
                if not blocked_started.wait(2):
                    raise AssertionError("blocked map call did not start")
                value = _LargeValue()
                value_reference.append(weakref.ref(value))
                return value
            blocked_started.set()
            if not release_blocked.wait(2):
                raise AssertionError("test did not release blocked map call")
            return "late"

        def interrupt_caller_wrap(_cls, value):
            if threading.get_ident() == caller_thread:
                value = None
                raise interruption
            return real_success(value)

        try:
            with mock.patch.object(
                CallOutcome,
                "success",
                new=classmethod(interrupt_caller_wrap),
            ):
                raised = _capture_error(
                    lambda: pool.map_until(
                        time.monotonic() + 1,
                        operation,
                        ("complete", "blocked"),
                    )
                )

            self.assertIs(raised, interruption)
            self.assertEqual(len(pool._active), 1)
            blocked_call = next(iter(pool._active))
            self.assertEqual(
                blocked_call._state.name,
                "ABANDONED_RUNNING",
            )
            self.assertEqual(blocked_call._abandonment.name, "INTERRUPTED")
            self.assertIsNone(blocked_call._outcome)

            probe = pool.submit(lambda: probe_entered.set())
            self.assertTrue(probe_entered.wait(1))
            probe.result_until(time.monotonic() + 1)
            gc.collect()
            self.assertEqual(len(value_reference), 1)
            self.assertIsNone(value_reference[0]())
        finally:
            release_blocked.set()
            pool.close_until(time.monotonic() + 0.2)

    def test_worker_loop_does_not_retain_a_discarded_late_value(self):
        pool = network_calls.BoundedDaemonPool(1, "late-value")
        started = threading.Event()
        release = threading.Event()
        probe_entered = threading.Event()
        release_probe = threading.Event()
        late_reference = []

        def operation():
            started.set()
            if not release.wait(2):
                raise AssertionError("test did not release operation")
            value = _LargeValue()
            late_reference.append(weakref.ref(value))
            return value

        try:
            call = pool.submit(operation)
            self.assertTrue(started.wait(1))
            with self.assertRaises(CallDeadlineExceeded):
                call.result_until(time.monotonic() + 0.01)
            release.set()
            self.assertTrue(
                _wait_until(
                    lambda: _token_is_returned(pool, call),
                    timeout=0.2,
                )
            )

            def probe_operation():
                probe_entered.set()
                if not release_probe.wait(2):
                    raise AssertionError("test did not release probe operation")

            probe = pool.submit(probe_operation)
            self.assertTrue(probe_entered.wait(1))
            self.assertEqual(len(late_reference), 1)
            gc.collect()
            self.assertIsNone(late_reference[0]())
            self.assertTrue(pool._workers[0].is_alive())
        finally:
            release.set()
            release_probe.set()
            pool.close_until(time.monotonic() + 0.2)

    def test_map_preserves_order_refills_capacity_and_uses_one_deadline(self):
        pool = network_calls.BoundedDaemonPool(2, "ordered-map")
        first_started = threading.Event()
        later_started = threading.Event()
        release_first = threading.Event()
        state_lock = threading.Lock()
        running = 0
        peak_running = 0
        observed_deadlines = []
        original_result_until = BoundedCall.result_until

        def operation(item):
            nonlocal running, peak_running
            with state_lock:
                running += 1
                peak_running = max(peak_running, running)
                if item == 2:
                    later_started.set()
            try:
                if item == 0:
                    first_started.set()
                    if not release_first.wait(2):
                        raise AssertionError("test did not release first item")
                return item * 10
            finally:
                with state_lock:
                    running -= 1

        def record_deadline(call, deadline):
            observed_deadlines.append(deadline)
            return original_result_until(call, deadline)

        deadline = time.monotonic() + 0.05
        try:
            with mock.patch.object(
                BoundedCall,
                "result_until",
                record_deadline,
            ):
                outcomes = pool.map_until(deadline, operation, range(5))
            self.assertTrue(first_started.is_set())
            self.assertTrue(later_started.is_set())
        finally:
            release_first.set()
            pool.close_until(time.monotonic() + 0.2)

        self.assertEqual(len(outcomes), 5)
        with self.assertRaises(CallDeadlineExceeded):
            outcomes[0].result()
        self.assertEqual(
            [outcome.result() for outcome in outcomes[1:]],
            [10, 20, 30, 40],
        )
        self.assertLessEqual(peak_running, 2)
        self.assertGreater(len(observed_deadlines), 0)
        self.assertEqual(set(observed_deadlines), {deadline})


class BoundedDaemonPoolLifecycleTests(unittest.TestCase):
    def _assert_abandoned_submission(self, pool, call):
        self.assertEqual(call._state.name, "ABANDONED_PENDING")
        self.assertEqual(call._abandonment.name, "INTERRUPTED")
        self.assertTrue(call._event.is_set())
        self.assertNotIn(call, pool._active)
        self.assertIsNone(pool._token_owners[call._token])
        self.assertIn(call._token, pool._available_tokens)
        self.assertEqual(_available_token_count(pool), pool.capacity)

    def test_submission_interruption_before_enqueue_abandons_call(self):
        pool = network_calls.BoundedDaemonPool(1, "before-enqueue")
        pool._startup_attempted = True
        pool._startup_succeeded = True
        interruption = _CallerAbort("interrupted before enqueue")
        try:
            with _record_constructed_calls() as calls, _raise_on_statement(
                network_calls.BoundedDaemonPool._submit_started,
                "self._work_queue.put_nowait(task)",
                interruption,
            ):
                raised = _capture_error(lambda: pool.submit(lambda: None))

            self.assertIs(raised, interruption)
            self.assertEqual(len(calls), 1)
            self._assert_abandoned_submission(pool, calls[0])
            self.assertTrue(pool._work_queue.empty())
        finally:
            pool.begin_close()

    def test_submission_enqueue_then_interruption_abandons_queued_call(self):
        pool = network_calls.BoundedDaemonPool(1, "after-enqueue")
        pool._startup_attempted = True
        pool._startup_succeeded = True
        interruption = _CallerAbort("interrupted after enqueue")
        real_put = pool._work_queue.put_nowait

        def enqueue_then_interrupt(task):
            real_put(task)
            raise interruption

        try:
            with _record_constructed_calls() as calls, mock.patch.object(
                pool._work_queue,
                "put_nowait",
                side_effect=enqueue_then_interrupt,
            ):
                raised = _capture_error(lambda: pool.submit(lambda: None))

            self.assertIs(raised, interruption)
            self.assertEqual(len(calls), 1)
            self._assert_abandoned_submission(pool, calls[0])
            self.assertIs(pool._work_queue.get_nowait().call, calls[0])
        finally:
            pool.begin_close()

    def test_submission_callback_interruption_repairs_registered_call(self):
        pool = network_calls.BoundedDaemonPool(1, "callback-interrupt")
        pool._startup_attempted = True
        pool._startup_succeeded = True
        interruption = _CallerAbort("registration callback interrupted")
        registered = []

        def register_then_interrupt(call):
            registered.append(call)
            raise interruption

        try:
            with _record_constructed_calls() as calls:
                raised = _capture_error(
                    lambda: pool._submit_started(
                        lambda: None,
                        (),
                        {},
                        on_enqueued=register_then_interrupt,
                    )
                )

            self.assertIs(raised, interruption)
            self.assertEqual(registered, calls)
            self.assertEqual(len(calls), 1)
            self._assert_abandoned_submission(pool, calls[0])
        finally:
            pool.begin_close()

    def test_map_return_interruption_repairs_registered_handoff(self):
        pool = network_calls.BoundedDaemonPool(1, "map-handoff-interrupt")
        pool._startup_attempted = True
        pool._startup_succeeded = True
        interruption = _CallerAbort("interrupted before submit return")
        try:
            with _record_constructed_calls() as calls, _raise_on_statement(
                network_calls.BoundedDaemonPool._submit_started,
                "return call",
                interruption,
            ):
                raised = _capture_error(
                    lambda: pool.map_until(
                        time.monotonic() + 1,
                        lambda item: item,
                        (1,),
                    )
                )

            self.assertIs(raised, interruption)
            self.assertEqual(len(calls), 1)
            self._assert_abandoned_submission(pool, calls[0])
        finally:
            pool.begin_close()

    def test_map_outer_cleanup_owns_completed_submission_callback(self):
        pool = network_calls.BoundedDaemonPool(1, "map-callback-owner")
        pool._startup_attempted = True
        pool._startup_succeeded = True
        interruption = _CallerAbort("interrupted after submission callback")
        submitted = []
        real_submit = pool._submit_started

        def interrupt_after_callback(
            function,
            args,
            kwargs,
            *,
            on_enqueued=None,
        ):
            self.assertIsNotNone(on_enqueued)
            call = real_submit(
                function,
                args,
                kwargs,
                on_enqueued=on_enqueued,
            )
            submitted.append(call)
            raise interruption

        try:
            with mock.patch.object(
                pool,
                "_submit_started",
                side_effect=interrupt_after_callback,
            ):
                raised = _capture_error(
                    lambda: pool.map_until(
                        time.monotonic() + 1,
                        lambda item: item,
                        (1,),
                    )
                )

            self.assertIs(raised, interruption)
            self.assertEqual(len(submitted), 1)
            self._assert_abandoned_submission(pool, submitted[0])
        finally:
            pool.begin_close()

    def test_queue_full_cleanup_retries_interrupted_token_return(self):
        pool = network_calls.BoundedDaemonPool(1, "queue-full-cleanup")
        pool._startup_attempted = True
        pool._startup_succeeded = True
        filler = object()
        pool._work_queue.put_nowait(filler)
        interruption = _CallerAbort("interrupted during queue-full cleanup")
        try:
            with _record_constructed_calls() as calls, _raise_on_statement(
                network_calls.BoundedDaemonPool._return_token_locked,
                "self._available_tokens.add(token)",
                interruption,
            ):
                raised = _capture_error(lambda: pool.submit(lambda: None))

            self.assertIs(raised, interruption)
            self.assertEqual(len(calls), 1)
            self._assert_abandoned_submission(pool, calls[0])
            self.assertIs(pool._work_queue.get_nowait(), filler)
        finally:
            if not pool._work_queue.empty():
                pool._work_queue.get_nowait()
            pool.begin_close()

    def test_constructor_cleanup_retries_interrupted_token_return(self):
        pool = network_calls.BoundedDaemonPool(1, "constructor-cleanup")
        pool._startup_attempted = True
        pool._startup_succeeded = True
        constructor_interruption = _CallerAbort("constructor interrupted")
        cleanup_interruption = _CallerAbort("constructor cleanup interrupted")

        class InterruptingAvailableTokens(set):
            def __init__(self, values):
                super().__init__(values)
                self.interrupted = False

            def add(self, token):
                if not self.interrupted:
                    self.interrupted = True
                    raise cleanup_interruption
                super().add(token)

        class InterruptingCall:
            @classmethod
            def __class_getitem__(cls, _item):
                return cls

            def __new__(cls, *_args, **_kwargs):
                raise constructor_interruption

        pool._available_tokens = InterruptingAvailableTokens(
            pool._available_tokens
        )
        try:
            with mock.patch.object(
                network_calls,
                "BoundedCall",
                InterruptingCall,
            ):
                raised = _capture_error(lambda: pool.submit(lambda: None))

            self.assertIs(raised, cleanup_interruption)
            self.assertEqual(len(pool._active), 0)
            self.assertEqual(_available_token_count(pool), 1)
            self.assertEqual(set(pool._token_owners.values()), {None})
        finally:
            pool.begin_close()

    def test_submission_interruption_before_guard_does_not_remove_token(self):
        pool = network_calls.BoundedDaemonPool(1, "pre-guard-token")
        pool._startup_attempted = True
        pool._startup_succeeded = True
        interruption = _CallerAbort("interrupted before submission guard")
        try:
            with _raise_on_statement(
                network_calls.BoundedDaemonPool._submit_started,
                "call = None",
                interruption,
            ):
                raised = _capture_error(lambda: pool.submit(lambda: None))

            self.assertIs(raised, interruption)
            self.assertEqual(len(pool._active), 0)
            self.assertEqual(_available_token_count(pool), 1)
            self.assertEqual(set(pool._token_owners.values()), {None})
            self.assertTrue(pool._work_queue.empty())
        finally:
            pool.begin_close()

    def test_enqueue_cleanup_retries_interrupted_token_return(self):
        pool = network_calls.BoundedDaemonPool(1, "enqueue-cleanup")
        pool._startup_attempted = True
        pool._startup_succeeded = True
        enqueue_interruption = _CallerAbort("enqueue interrupted")
        cleanup_interruption = _CallerAbort("enqueue cleanup interrupted")
        real_put = pool._work_queue.put_nowait

        def enqueue_then_interrupt(task):
            real_put(task)
            raise enqueue_interruption

        try:
            with _record_constructed_calls() as calls, mock.patch.object(
                pool._work_queue,
                "put_nowait",
                side_effect=enqueue_then_interrupt,
            ), _raise_on_statement(
                network_calls.BoundedDaemonPool._return_token_locked,
                "self._available_tokens.add(token)",
                cleanup_interruption,
            ):
                raised = _capture_error(lambda: pool.submit(lambda: None))

            self.assertIs(raised, cleanup_interruption)
            self.assertEqual(len(calls), 1)
            self._assert_abandoned_submission(pool, calls[0])
        finally:
            pool.begin_close()

    def test_identity_tokens_bound_admission_and_return_by_owner(self):
        pool = network_calls.BoundedDaemonPool(1, "identity-token")
        self.assertTrue(
            hasattr(pool, "_available_tokens"),
            "pool must expose identity-token capacity",
        )
        started = threading.Event()
        release = threading.Event()

        def block():
            started.set()
            if not release.wait(2):
                raise AssertionError("test did not release token owner")
            return "complete"

        try:
            self.assertEqual(len(pool._available_tokens), 1)
            self.assertEqual(len(pool._token_owners), 1)
            self.assertEqual(set(pool._token_owners.values()), {None})

            call = pool.submit(block)
            self.assertTrue(started.wait(1))
            self.assertEqual(len(pool._available_tokens), 0)
            self.assertIs(pool._token_owners[call._token], call)
            with self.assertRaises(PoolSaturatedError):
                pool.submit(lambda: None)

            release.set()
            self.assertEqual(
                call.result_until(time.monotonic() + 1),
                "complete",
            )
            self.assertEqual(len(pool._available_tokens), 1)
            self.assertIsNone(pool._token_owners[call._token])
            self.assertEqual(len(pool._active), 0)
        finally:
            release.set()
            pool.close_until(time.monotonic() + 0.2)

    def test_registry_pools_start_independently(self):
        with installed_network_pools() as pools:
            self.assertIs(network_calls.get_network_call_pools(), pools)
            self.assertEqual(pools.dns._workers, [])
            self.assertEqual(pools.http._workers, [])
            self.assertEqual(pools.icmp._workers, [])

            value = object()
            self.assertIs(
                pools.dns.call_until(
                    time.monotonic() + 1,
                    lambda: value,
                ),
                value,
            )

            self.assertEqual(len(pools.dns._workers), 4)
            self.assertTrue(
                all(worker.daemon for worker in pools.dns._workers)
            )
            self.assertEqual(
                {worker.name for worker in pools.dns._workers},
                {"obozrenie-dns-resolver"},
            )
            self.assertTrue(
                all(worker.is_alive() for worker in pools.dns._workers)
            )
            self.assertEqual(pools.http._workers, [])
            self.assertEqual(pools.icmp._workers, [])

        self.assertTrue(
            all(not worker.is_alive() for worker in pools.dns._workers)
        )

    def test_partial_startup_failure_is_sticky(self):
        real_thread = threading.Thread
        for successful_launches in (1, 2, 3):
            with self.subTest(successful_launches=successful_launches):
                pool = network_calls.BoundedDaemonPool(4, "partial-worker")
                initial_error = _WorkerAbort(
                    f"construction stopped after {successful_launches} launches"
                )
                thread_options = []

                def make_thread(*args, **kwargs):
                    thread_options.append(kwargs)
                    if len(thread_options) > successful_launches:
                        raise initial_error
                    return real_thread(*args, **kwargs)

                try:
                    with mock.patch.object(
                        network_calls.threading,
                        "Thread",
                        side_effect=make_thread,
                    ):
                        first_error = _capture_error(pool.ensure_started)
                        later_errors = [
                            _capture_error(pool.ensure_started)
                            for _index in range(2)
                        ]

                    self.assertIs(first_error, initial_error)
                    self.assertTrue(pool.startup_failed)
                    self.assertEqual(
                        len(thread_options), successful_launches + 1
                    )
                    self.assertEqual(
                        len(pool._workers), successful_launches
                    )
                    self.assertTrue(
                        all(options["daemon"] for options in thread_options)
                    )
                    self.assertEqual(
                        {options["name"] for options in thread_options},
                        {"partial-worker"},
                    )
                    self.assertEqual(len({id(error) for error in later_errors}), 2)
                    self.assertTrue(
                        all(
                            type(error) is PoolUnavailableError
                            for error in later_errors
                        )
                    )
                    self.assertTrue(
                        all(error is not initial_error for error in later_errors)
                    )
                finally:
                    pool.close_until(time.monotonic() + 0.2)

                self.assertTrue(
                    all(not worker.is_alive() for worker in pool._workers)
                )

    def test_initial_startup_base_exception_is_raw_then_generic(self):
        pool = network_calls.BoundedDaemonPool(1, "failed-worker")
        payload = bytearray(1024 * 1024)
        initial_error = _WorkerAbort("startup interrupted", payload)

        with mock.patch.object(
            network_calls.threading,
            "Thread",
            side_effect=initial_error,
        ) as thread_factory:
            first_error = _capture_error(pool.ensure_started)
            later_errors = [
                _capture_error(pool.ensure_started) for _index in range(3)
            ]

        self.assertIs(first_error, initial_error)
        self.assertEqual(thread_factory.call_count, 1)
        self.assertTrue(pool.startup_failed)
        self.assertEqual(pool._workers, [])
        self.assertEqual(len({id(error) for error in later_errors}), 3)
        self.assertTrue(
            all(type(error) is PoolUnavailableError for error in later_errors)
        )
        self.assertTrue(
            all(error is not initial_error for error in later_errors)
        )
        self.assertFalse(
            any(
                isinstance(value, BaseException)
                for value in vars(pool).values()
            )
        )

    def test_admission_and_queue_saturation_are_nonblocking(self):
        with self.subTest(resource="admission"):
            pool = network_calls.BoundedDaemonPool(1, "admission-worker")
            started = threading.Event()
            release = threading.Event()

            def block():
                started.set()
                if not release.wait(2):
                    raise AssertionError("test did not release admission worker")

            try:
                first_call = pool.submit(block)
                self.assertTrue(started.wait(1))
                self.assertEqual(len(pool._active), 1)
                self.assertEqual(pool._work_queue.qsize(), 0)
                self.assertEqual(_available_token_count(pool), 0)
            finally:
                release.set()
                pool.close_until(time.monotonic() + 0.2)
            self.assertTrue(_token_is_returned(pool, first_call))
            self.assertEqual(_available_token_count(pool), 1)

        with self.subTest(resource="available token mutation"):
            pool = network_calls.BoundedDaemonPool(1, "manual-admission")
            pool._startup_attempted = True
            pool._startup_succeeded = True
            reservation = object()
            with pool._state_lock:
                token = pool._available_tokens.pop()
                pool._token_owners[token] = reservation
            errors = []
            finished = threading.Event()

            def submit_without_capacity():
                try:
                    pool.submit(lambda: None)
                except BaseException as error:
                    errors.append(error)
                finally:
                    finished.set()

            caller = threading.Thread(target=submit_without_capacity)
            caller.start()
            try:
                completed_before_release = finished.wait(0.1)
            finally:
                with pool._state_lock:
                    pool._available_tokens.add(token)
                    pool._token_owners[token] = None
                caller.join(1)
                pool.begin_close()

            self.assertTrue(completed_before_release)
            self.assertFalse(caller.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIs(type(errors[0]), PoolSaturatedError)
            self.assertEqual(len(pool._active), 0)
            self.assertEqual(_available_token_count(pool), 1)

        with self.subTest(resource="work queue"):
            pool = network_calls.BoundedDaemonPool(1, "manual-queue")
            pool._startup_attempted = True
            pool._startup_succeeded = True
            filler = object()
            pool._work_queue.put_nowait(filler)
            errors = []
            calls = []
            finished = threading.Event()

            def submit_to_full_queue():
                try:
                    calls.append(pool.submit(lambda: None))
                except BaseException as error:
                    errors.append(error)
                finally:
                    finished.set()

            caller = threading.Thread(target=submit_to_full_queue)
            caller.start()
            try:
                completed_before_drain = finished.wait(0.1)
            finally:
                try:
                    queued = pool._work_queue.get_nowait()
                except queue.Empty:
                    queued = None
                caller.join(1)
                pool.begin_close()

            self.assertTrue(completed_before_drain)
            self.assertIs(queued, filler)
            self.assertFalse(caller.is_alive())
            self.assertEqual(calls, [])
            self.assertEqual(len(errors), 1)
            self.assertIs(type(errors[0]), PoolSaturatedError)
            self.assertEqual(len(pool._active), 0)
            self.assertEqual(_available_token_count(pool), 1)

    def test_close_wakes_active_and_drains_queued_calls(self):
        class IdleThread:
            ident = None

            def __init__(self, *_args, **_kwargs):
                pass

            def start(self):
                pass

            def join(self, _timeout=None):
                pass

        pool = network_calls.BoundedDaemonPool(2, "close-worker")
        with mock.patch.object(network_calls.threading, "Thread", IdleThread):
            pool.ensure_started()
            calls = [pool.submit(lambda: None) for _index in range(2)]
            self.assertEqual(len(pool._active), 2)
            self.assertEqual(pool._work_queue.qsize(), 2)
            self.assertEqual(_available_token_count(pool), 0)

            pool.begin_close()

        errors = [
            _capture_error(
                lambda call=call: call.result_until(time.monotonic() + 1)
            )
            for call in calls
        ]
        queued_items = []
        while True:
            try:
                queued_items.append(pool._work_queue.get_nowait())
            except queue.Empty:
                break

        self.assertEqual(len(errors), 2)
        self.assertEqual(len({id(error) for error in errors}), 2)
        self.assertTrue(
            all(type(error) is PoolUnavailableError for error in errors)
        )
        self.assertEqual(len(pool._active), 0)
        self.assertEqual(_available_token_count(pool), 2)
        self.assertEqual(queued_items, [])

        running_pool = network_calls.BoundedDaemonPool(1, "running-close")
        started = threading.Event()
        release = threading.Event()
        caller_finished = threading.Event()
        caller_outcomes = []

        def block():
            started.set()
            if not release.wait(2):
                raise AssertionError("test did not release running close")

        running_call = running_pool.submit(block)

        def await_running_call():
            caller_outcomes.append(
                _capture_value_or_error(
                    lambda: running_call.result_until(time.monotonic() + 1)
                )
            )
            caller_finished.set()

        caller = threading.Thread(target=await_running_call)
        caller.start()
        try:
            self.assertTrue(started.wait(1))
            running_pool.begin_close()
            completed_before_release = caller_finished.wait(0.1)
        finally:
            release.set()
            caller.join(1)
            running_pool.close_until(time.monotonic() + 0.2)

        self.assertTrue(completed_before_release)
        self.assertFalse(caller.is_alive())
        self.assertEqual(len(caller_outcomes), 1)
        succeeded, outcome = caller_outcomes[0]
        self.assertFalse(succeeded)
        self.assertIs(type(outcome), PoolUnavailableError)
        self.assertEqual(len(running_pool._active), 0)
        self.assertEqual(_available_token_count(running_pool), 1)

    def test_begin_close_retries_after_closed_flag_interruption(self):
        pool = network_calls.BoundedDaemonPool(1, "close-flag-retry")
        pool._startup_attempted = True
        pool._startup_succeeded = True
        call = pool.submit(lambda: None)
        interruption = _CallerAbort("interrupted after closed flag")

        with _raise_on_statement(
            network_calls.BoundedDaemonPool.begin_close,
            "for call in tuple(self._active):",
            interruption,
        ):
            raised = _capture_error(pool.begin_close)

        self.assertIs(raised, interruption)
        self.assertTrue(pool._closed)
        self.assertFalse(getattr(pool, "_close_complete", False))

        pool.begin_close()

        self.assertTrue(getattr(pool, "_close_complete", False))
        self.assertEqual(call._state.name, "ABANDONED_PENDING")
        self.assertEqual(call._abandonment.name, "CLOSED")
        self.assertTrue(call._event.is_set())
        self.assertNotIn(call, pool._active)
        self.assertEqual(_available_token_count(pool), 1)
        self.assertTrue(pool._work_queue.empty())

    def test_begin_close_replaces_partial_sentinel_batch_on_retry(self):
        class LiveWorker:
            ident = 1

            def is_alive(self):
                return True

            def join(self, _timeout=None):
                pass

        pool = network_calls.BoundedDaemonPool(2, "sentinel-retry")
        pool._workers = [LiveWorker(), LiveWorker()]
        interruption = _CallerAbort("sentinel insertion interrupted")
        real_put = pool._work_queue.put_nowait
        put_count = 0

        def interrupt_second_put(item):
            nonlocal put_count
            put_count += 1
            if put_count == 2:
                raise interruption
            real_put(item)

        try:
            with mock.patch.object(
                pool._work_queue,
                "put_nowait",
                side_effect=interrupt_second_put,
            ):
                raised = _capture_error(pool.begin_close)

            self.assertIs(raised, interruption)
            self.assertFalse(getattr(pool, "_close_complete", False))
            self.assertEqual(pool._work_queue.qsize(), 1)

            pool.begin_close()

            self.assertTrue(getattr(pool, "_close_complete", False))
            queued_items = [
                pool._work_queue.get_nowait() for _index in range(2)
            ]
            self.assertTrue(pool._work_queue.empty())
            self.assertTrue(
                all(
                    item is network_calls._WORKER_STOP
                    for item in queued_items
                )
            )
        finally:
            while not pool._work_queue.empty():
                pool._work_queue.get_nowait()

    def test_begin_close_retries_interruption_before_complete_marker(self):
        class LiveWorker:
            ident = 1

            def is_alive(self):
                return True

            def join(self, _timeout=None):
                pass

        pool = network_calls.BoundedDaemonPool(1, "close-marker-retry")
        pool._workers = [LiveWorker()]
        interruption = _CallerAbort("interrupted before close completion")
        try:
            with _raise_on_statement(
                network_calls.BoundedDaemonPool.begin_close,
                "self._close_complete = True",
                interruption,
            ):
                raised = _capture_error(pool.begin_close)

            self.assertIs(raised, interruption)
            self.assertFalse(pool._close_complete)
            self.assertEqual(pool._work_queue.qsize(), 1)

            pool.begin_close()

            self.assertTrue(pool._close_complete)
            self.assertIs(
                pool._work_queue.get_nowait(),
                network_calls._WORKER_STOP,
            )
            self.assertTrue(pool._work_queue.empty())
        finally:
            while not pool._work_queue.empty():
                pool._work_queue.get_nowait()

    def test_begin_close_targets_only_live_launched_workers(self):
        class Worker:
            def __init__(self, ident, alive):
                self.ident = ident
                self._alive = alive

            def is_alive(self):
                return self._alive

            def join(self, _timeout=None):
                pass

        pool = network_calls.BoundedDaemonPool(3, "live-close-workers")
        pool._workers = [
            Worker(1, True),
            Worker(2, False),
            Worker(None, False),
        ]
        try:
            pool.begin_close()
            self.assertTrue(getattr(pool, "_close_complete", False))
            self.assertIs(
                pool._work_queue.get_nowait(),
                network_calls._WORKER_STOP,
            )
            self.assertTrue(pool._work_queue.empty())
        finally:
            while not pool._work_queue.empty():
                pool._work_queue.get_nowait()

    def test_close_races_return_each_token_once(self):
        pool = network_calls.BoundedDaemonPool(1, "close-race-worker")
        started = threading.Event()
        release = threading.Event()
        caller_finished = threading.Event()
        errors = []

        def operation():
            started.set()
            if not release.wait(2):
                raise AssertionError("test did not release close race")
            return "late"

        call = pool.submit(operation)

        def await_call():
            errors.append(
                _capture_error(
                    lambda: call.result_until(time.monotonic() + 0.01)
                )
            )
            caller_finished.set()

        caller = threading.Thread(target=await_call)
        closer = threading.Thread(target=pool.begin_close)
        caller.start()
        try:
            self.assertTrue(started.wait(1))
            closer.start()
            self.assertTrue(caller_finished.wait(1))
        finally:
            release.set()
            caller.join(1)
            closer.join(1)
            pool.close_until(time.monotonic() + 0.2)

        self.assertFalse(caller.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(
            errors[0],
            (CallDeadlineExceeded, PoolUnavailableError),
        )
        self.assertTrue(_token_is_returned(pool, call))
        self.assertEqual(len(pool._active), 0)
        self.assertEqual(_available_token_count(pool), 1)

    def test_registry_close_marks_all_before_shared_deadline_joins(self):
        events = []
        join_deadlines = []

        class FakeClock:
            def __init__(self):
                self.value = 100.0

            def monotonic(self):
                return self.value

            def advance(self):
                self.value += 1.0

        clock = FakeClock()

        class RecordingPool:
            def __init__(self, name):
                self.name = name

            def begin_close(self):
                events.append(f"{self.name}-begin")
                clock.advance()

            def join_until(self, deadline):
                events.append(f"{self.name}-join")
                join_deadlines.append(deadline)

        pools = network_calls.NetworkCallPools(
            dns=RecordingPool("dns"),
            http=RecordingPool("http"),
            icmp=RecordingPool("icmp"),
        )
        timeout = 7.0
        deadline_before_any_begin = clock.value + timeout
        with mock.patch.object(network_calls, "_NETWORK_CALL_POOLS", pools), mock.patch.object(
            network_calls.time,
            "monotonic",
            side_effect=clock.monotonic,
        ):
            network_calls.close_network_call_pools(timeout=timeout)

        self.assertEqual(
            events[:3],
            ["dns-begin", "http-begin", "icmp-begin"],
        )
        self.assertEqual(
            events[3:],
            ["dns-join", "http-join", "icmp-join"],
        )
        self.assertEqual(len(set(join_deadlines)), 1)
        self.assertEqual(join_deadlines[0], deadline_before_any_begin)

    def test_registry_close_retries_all_incomplete_pools_and_joins_all(self):
        events = []
        join_deadlines = []
        first_interruption = _CallerAbort("dns close interrupted")
        later_begin_interruption = _CallerAbort("icmp close interrupted")
        join_interruption = _CallerAbort("http join interrupted")

        class RecordingPool:
            def __init__(self, name, begin_error=None, join_error=None):
                self.name = name
                self._begin_error = begin_error
                self._join_error = join_error
                self._begin_count = 0
                self._close_complete = False

            def begin_close(self):
                self._begin_count += 1
                events.append(f"{self.name}-begin-{self._begin_count}")
                if self._begin_count == 1 and self._begin_error is not None:
                    raise self._begin_error
                self._close_complete = True

            def join_until(self, deadline):
                events.append(f"{self.name}-join")
                join_deadlines.append(deadline)
                if self._join_error is not None:
                    raise self._join_error

        pools = network_calls.NetworkCallPools(
            dns=RecordingPool("dns", begin_error=first_interruption),
            http=RecordingPool("http", join_error=join_interruption),
            icmp=RecordingPool(
                "icmp",
                begin_error=later_begin_interruption,
            ),
        )
        with mock.patch.object(network_calls, "_NETWORK_CALL_POOLS", pools):
            raised = _capture_error(
                lambda: network_calls.close_network_call_pools(timeout=0.5)
            )

        self.assertIs(raised, first_interruption)
        self.assertEqual(
            events,
            [
                "dns-begin-1",
                "http-begin-1",
                "icmp-begin-1",
                "dns-begin-2",
                "icmp-begin-2",
                "dns-join",
                "http-join",
                "icmp-join",
            ],
        )
        self.assertEqual(len(set(join_deadlines)), 1)

    def test_registry_close_continues_all_joins_after_join_interruption(self):
        events = []
        interruption = _CallerAbort("first join interrupted")

        class RecordingPool:
            def __init__(self, name, join_error=None):
                self.name = name
                self._join_error = join_error

            def begin_close(self):
                events.append(f"{self.name}-begin")

            def join_until(self, _deadline):
                events.append(f"{self.name}-join")
                if self._join_error is not None:
                    raise self._join_error

        pools = network_calls.NetworkCallPools(
            dns=RecordingPool("dns", join_error=interruption),
            http=RecordingPool("http"),
            icmp=RecordingPool("icmp"),
        )
        with mock.patch.object(network_calls, "_NETWORK_CALL_POOLS", pools):
            raised = _capture_error(network_calls.close_network_call_pools)

        self.assertIs(raised, interruption)
        self.assertEqual(
            events,
            [
                "dns-begin",
                "http-begin",
                "icmp-begin",
                "dns-join",
                "http-join",
                "icmp-join",
            ],
        )

    def test_daemon_worker_does_not_block_subprocess_exit(self):
        child = """
import threading
import time

from obozrenie.network_calls import BoundedDaemonPool, CallDeadlineExceeded

pool = BoundedDaemonPool(1, "subprocess-blocker")
blocked = threading.Event()
try:
    pool.call_until(time.monotonic() + 0.02, blocked.wait)
except CallDeadlineExceeded:
    raise SystemExit(0)
raise SystemExit(2)
"""
        command = [sys.executable, "-c", child]
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            stdout, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=1)
            self.fail(f"daemon worker blocked exit: {stdout!r} {stderr!r}")
        self.assertEqual(process.returncode, 0, stderr)


def _capture_value_or_error(callback):
    try:
        return True, callback()
    except BaseException as error:
        return False, error


if __name__ == "__main__":
    unittest.main()
