import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from server.plugins import contract, launcher, registry
from server.plugins.client import PluginProblem, request_json
from server.plugins.errors import PluginCrashed, PluginError, PluginUnavailable, UnsupportedOperation

logger = logging.getLogger(__name__)


def _raise_if_cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise PluginError("request_cancelled", "caller abandoned the request", status_code=499)


def _acquire(lock, cancel: threading.Event | None) -> None:
    while not lock.acquire(timeout=0.05):
        _raise_if_cancelled(cancel)


@dataclass(frozen=True)
class HostLimits:
    ready_seconds: float = contract.SHORT_SECONDS
    stop_grace_seconds: float = 5.0
    idle_seconds: float = 600.0
    max_concurrency: int = 8


@dataclass
class _Slot:
    start_lock: threading.RLock = field(default_factory=threading.RLock)
    condition: threading.Condition = field(default_factory=threading.Condition)
    running: launcher.Running | None = None
    state: str | None = None
    state_entry_sha256: str | None = None
    semaphore: threading.BoundedSemaphore | None = None
    in_flight: int = 0
    admitting: bool = True
    last_completed: float = 0.0


class PluginHost:
    def __init__(
        self,
        *,
        data_dir: Callable[[], str],
        locale: str = "en",
        limits: HostLimits | None = None,
    ) -> None:
        self._data_dir = data_dir
        self._locale = locale
        self._limits = limits or HostLimits()
        self._registry_lock = threading.Lock()
        self._slots_lock = threading.Lock()
        self._slots: dict[str, _Slot] = {}
        self._file_sha256: str | None = None
        self._snapshot = registry.Snapshot(file_sha256=None)
        self._last_valid = self._snapshot
        self._read_once = False

    def _slot(self, plugin_id: str) -> _Slot:
        with self._slots_lock:
            return self._slots.setdefault(plugin_id, _Slot())

    def _drain_and_stop(
        self, plugin_id: str, slot: _Slot, *, state: str | None, only: launcher.Running | None = None
    ) -> None:
        with slot.start_lock:
            with slot.condition:
                if only is not None and slot.running is not only:
                    return
                slot.admitting = False
                while slot.in_flight:
                    slot.condition.wait()
                running, slot.running = slot.running, None
                slot.semaphore = None
                slot.state = state
                slot.admitting = True
                slot.condition.notify_all()
            if running is not None and running.process is not None:
                launcher.stop_process(running.process, self._limits.stop_grace_seconds)
                logger.info("Stopped plug-in %s", plugin_id)

    def refresh(self) -> registry.Snapshot:
        to_stop: list[tuple[str, _Slot, launcher.Running]] = []
        with self._registry_lock:
            current = registry.registry_file_sha256()
            if self._read_once and current == self._file_sha256:
                return self._snapshot
            snapshot = registry.read_registry()
            self._read_once = True
            self._file_sha256 = current
            with self._slots_lock:
                slots = dict(self._slots)
            for plugin_id, slot in slots.items():
                entry = snapshot.entries.get(plugin_id)
                with slot.condition:
                    running = slot.running
                    if running is not None and not self._still_current(running, entry):
                        to_stop.append((plugin_id, slot, running))
                    elif entry is None or entry.entry_sha256 != slot.state_entry_sha256:
                        slot.state = None
            self._snapshot = snapshot
            if snapshot.valid:
                self._last_valid = snapshot
        for plugin_id, slot, running in to_stop:
            threading.Thread(
                target=self._drain_and_stop,
                args=(plugin_id, slot),
                kwargs={"state": None, "only": running},
                daemon=True,
            ).start()
        return snapshot

    @staticmethod
    def _still_current(running: launcher.Running, entry: registry.Entry | None) -> bool:
        return entry is not None and entry.confirmed and entry.entry_sha256 == running.entry.entry_sha256

    def status(self) -> list[dict]:
        snapshot = registry.read_registry()
        with self._registry_lock:
            if snapshot.valid:
                self._last_valid = snapshot
            last_valid = self._last_valid
        with self._slots_lock:
            running = any(slot.running is not None for slot in self._slots.values())
        if running and snapshot.file_sha256 != self._file_sha256:
            threading.Thread(target=self.refresh, daemon=True).start()
        listed = snapshot.entries if snapshot.valid else last_valid.entries
        result = []
        for plugin_id, entry in sorted(listed.items()):
            result.append(
                {
                    "id": entry.id,
                    "family": entry.family,
                    "binding": dict(entry.binding),
                    "version": entry.version,
                    "contract_version": entry.contract_version,
                    "contract_revision": entry.contract_revision,
                    "state": self._state(plugin_id, entry) if snapshot.valid else "unavailable",
                }
            )
        return result

    def _state(self, plugin_id: str, entry: registry.Entry) -> str:
        with self._slots_lock:
            slot = self._slots.get(plugin_id)
        if slot is not None:
            with slot.condition:
                running, state, state_entry = slot.running, slot.state, slot.state_entry_sha256
            if running is not None and running.entry.entry_sha256 == entry.entry_sha256:
                return "ready" if running.alive() else "crashed"
        if not entry.confirmed:
            return "unconfirmed"
        if not contract.contract_matches(entry.family, entry.contract_revision):
            return "incompatible_contract"
        if slot is not None and state is not None and state_entry == entry.entry_sha256:
            return state
        return "confirmed"

    def _start(self, entry: registry.Entry) -> launcher.Running:
        limits = launcher.StartLimits(
            ready_seconds=self._limits.ready_seconds, stop_grace_seconds=self._limits.stop_grace_seconds
        )
        for attempt in (1, 2):
            try:
                return launcher.start(entry, data_dir=self._data_dir(), locale=self._locale, limits=limits)
            except launcher.StartFailure as exc:
                if not exc.retryable or attempt == 2:
                    raise
                logger.warning("Plug-in %s did not start (%s); starting it once more", entry.id, exc)
        raise AssertionError("unreachable")

    def _ensure_running(self, plugin_id: str, cancel: threading.Event | None = None) -> tuple[_Slot, launcher.Running]:
        snapshot = self.refresh()
        if not snapshot.valid:
            raise PluginUnavailable("unavailable")
        if snapshot.entries.get(plugin_id) is None:
            raise PluginUnavailable("not_registered")
        slot = self._slot(plugin_id)
        while True:
            _acquire(slot.start_lock, cancel)
            try:
                entry = registry.read_registry().entries.get(plugin_id)
                if entry is None:
                    raise PluginUnavailable("unavailable", "the registry changed before the start")
                stale = slot.running
                if stale is None:
                    _raise_if_cancelled(cancel)
                    return slot, self._start_and_publish(plugin_id, slot, entry)
                if stale.alive() and self._still_current(stale, entry):
                    return slot, stale
            finally:
                slot.start_lock.release()
            drain = threading.Thread(
                target=self._drain_and_stop,
                args=(plugin_id, slot),
                kwargs={"state": None if stale.alive() else "crashed", "only": stale},
                daemon=True,
            )
            drain.start()
            while drain.is_alive():
                drain.join(0.05)
                _raise_if_cancelled(cancel)

    def _start_and_publish(self, plugin_id: str, slot: _Slot, entry: registry.Entry) -> launcher.Running:
        try:
            running = self._start(entry)
        except PluginError as exc:
            if exc.code != "unconfirmed":
                with slot.condition:
                    slot.state = exc.code
                    slot.state_entry_sha256 = entry.entry_sha256
            raise PluginUnavailable(exc.code, str(exc)) from exc
        if not self._still_current(running, registry.read_registry().entries.get(plugin_id)):
            if running.process is not None:
                launcher.stop_process(running.process, self._limits.stop_grace_seconds)
            raise PluginUnavailable("unavailable", "the registry changed during the start")
        max_concurrency = min(running.manifest["capabilities"]["max_concurrency"], self._limits.max_concurrency)
        with slot.condition:
            slot.running = running
            slot.state = "ready"
            slot.state_entry_sha256 = entry.entry_sha256
            slot.semaphore = threading.BoundedSemaphore(max_concurrency)
            slot.last_completed = time.monotonic()
        logger.info("Started plug-in %s %s", entry.id, entry.version)
        self._schedule_idle_stop(plugin_id, slot, running, self._limits.idle_seconds)
        return running

    def ensure_started(self, plugin_id: str) -> str:
        try:
            self._ensure_running(plugin_id)
        except PluginError as exc:
            return exc.code
        return "ready"

    def _schedule_idle_stop(self, plugin_id: str, slot: _Slot, running: launcher.Running, delay: float) -> None:
        timer = threading.Timer(delay, self._idle_stop, args=(plugin_id, slot, running))
        timer.daemon = True
        timer.start()

    def _idle_stop(self, plugin_id: str, slot: _Slot, running: launcher.Running) -> None:
        with slot.condition:
            if slot.running is not running:
                return
            idle_for = time.monotonic() - slot.last_completed
            delay = self._limits.idle_seconds if slot.in_flight else self._limits.idle_seconds - idle_for
        if delay > 0:
            self._schedule_idle_stop(plugin_id, slot, running, delay)
            return
        with slot.start_lock:
            with slot.condition:
                if slot.running is not running:
                    return
                idle_for = time.monotonic() - slot.last_completed
                if slot.in_flight or idle_for < self._limits.idle_seconds:
                    remaining = self._limits.idle_seconds - (0 if slot.in_flight else idle_for)
                    self._schedule_idle_stop(plugin_id, slot, running, remaining)
                    return
                slot.admitting = False
            self._drain_and_stop(plugin_id, slot, state=None, only=running)

    def call(
        self,
        plugin_id: str,
        operation: str,
        body: dict,
        *,
        cancel: threading.Event | None = None,
    ) -> object:
        for _ in range(2):
            slot, running = self._ensure_running(plugin_id, cancel)
            if operation not in running.manifest["capabilities"]["operations"]:
                raise UnsupportedOperation(operation)
            with slot.condition:
                while not slot.admitting:
                    slot.condition.wait(0.05)
                    _raise_if_cancelled(cancel)
                if slot.running is running and slot.semaphore is not None:
                    semaphore = slot.semaphore
                    slot.in_flight += 1
                    break
        else:
            raise PluginUnavailable("unavailable", "plug-in stopped before the request")
        lost_endpoint = False
        try:
            _acquire(semaphore, cancel)
            try:
                return request_json(
                    running.endpoint,
                    "POST",
                    contract.operation_path(operation),
                    body,
                    timeout=contract.operation_timeout(running.entry.family, operation, body),
                    cancel=cancel,
                )
            finally:
                semaphore.release()
        except PluginCrashed:
            lost_endpoint = running.process is None
            if running.process is not None:
                deadline = time.monotonic() + self._limits.stop_grace_seconds
                while running.process.poll() is None and time.monotonic() < deadline:
                    _raise_if_cancelled(cancel)
                    time.sleep(0.05)
            raise
        except PluginProblem as exc:
            lost_endpoint = running.process is None and exc.http_status == 401
            raise
        finally:
            with slot.condition:
                slot.in_flight -= 1
                slot.last_completed = time.monotonic()
                slot.condition.notify_all()
            if lost_endpoint:
                threading.Thread(
                    target=self._drain_and_stop,
                    args=(plugin_id, slot),
                    kwargs={"state": "crashed", "only": running},
                    daemon=True,
                ).start()

    def stop_all(self) -> None:
        with self._slots_lock:
            slots = dict(self._slots)
        for plugin_id, slot in slots.items():
            self._drain_and_stop(plugin_id, slot, state=None)
