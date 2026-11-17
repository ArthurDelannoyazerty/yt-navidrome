"""Executable workflow contracts, shared by services and SQLite constraints.

Temporal state is separate from identity, configuration, and counters. Machines
are small immutable transition tables; persistent transitions remain inside the
same SQLite transaction as the business data they protect. No FSM library owns
our connections, background tasks, or recovery policy.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping


class InvalidTransition(ValueError):
    """An event is not legal in the current workflow state."""


@dataclass(frozen=True)
class Machine:
    name: str
    initial: str
    transitions: Mapping[str, Mapping[str, str]]

    def __post_init__(self):
        graph = {str(state): MappingProxyType(dict(events))
                 for state, events in self.transitions.items()}
        if self.initial not in graph or any(
            target not in graph for events in graph.values() for target in events.values()
        ):
            raise ValueError(f"Invalid machine definition: {self.name}")
        object.__setattr__(self, "transitions", MappingProxyType(graph))

    def send(self, state: str, event: str) -> str:
        try:
            return self.transitions[state][event]
        except KeyError:
            raise InvalidTransition(f"{self.name}: cannot {event!r} from {state!r}") from None

    def allows(self, before: str, after: str) -> bool:
        return before in self.transitions and (
            before == after or after in self.transitions[before].values()
        )

    def require(self, before: str, after: str) -> str:
        if not self.allows(before, after):
            raise InvalidTransition(f"{self.name}: {before!r} -> {after!r} is not allowed")
        return after

    def describe(self) -> dict:
        return {"name": self.name, "initial": self.initial,
                "states": list(self.transitions),
                "transitions": [{"from": state, "event": event, "to": target}
                                for state, events in self.transitions.items()
                                for event, target in events.items()]}


def machine(name: str, initial: str, graph: dict[str, dict[str, str]]) -> Machine:
    return Machine(name, initial, graph)


JOB = machine("job", "PENDING", {
    "PENDING": {"claim": "RUNNING", "cancel": "CANCELLED", "fail": "FAILED",
                "complete_without_work": "DONE"},
    "RUNNING": {"finish": "DONE", "fail": "FAILED", "cancel": "CANCELLED",
                "defer": "PENDING", "recover": "PENDING"},
    "DONE": {}, "FAILED": {}, "CANCELLED": {},
})

# The persisted queue uses PENDING + not_before; this named projection is the
# authoritative scheduler/cancellation state, not another independently writable
# state column. A deadline is data, not an unbounded set of machine states.
class JobPhase(StrEnum):
    QUEUED = "QUEUED"
    DEFERRED = "DEFERRED"
    RUNNING = "RUNNING"
    COMMITTING = "COMMITTING"
    CANCELLING = "CANCELLING"
    DRAINING = "DRAINING"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


JOB_CONTROL = machine("job_control", "QUEUED", {
    "QUEUED": {"claim": "RUNNING", "defer": "DEFERRED", "cancel": "CANCELLED"},
    "DEFERRED": {"deadline": "QUEUED", "cancel": "CANCELLED"},
    "RUNNING": {"commit": "COMMITTING", "request_cancel": "CANCELLING",
                "defer": "DEFERRED", "finish": "DONE", "fail": "FAILED"},
    "COMMITTING": {"commit_done": "RUNNING", "defer_cleanup": "DEFERRED",
                   "request_stop_after_commit": "DRAINING", "finish": "DONE", "fail": "FAILED"},
    "DRAINING": {"safe_point": "CANCELLING", "finish": "DONE", "defer_cleanup": "DEFERRED"},
    "CANCELLING": {"acknowledge": "CANCELLED"},
    "DONE": {}, "FAILED": {}, "CANCELLED": {},
})
COMMIT_STAGES = frozenset({"applying", "publishing"})
JOB_STAGES = frozenset({"queued", "resolving", "downloading", "identifying", *COMMIT_STAGES})


def job_phase(job: Mapping, now: float, *, committed: bool = False) -> JobPhase:
    state = job["state"]
    if state in {"DONE", "FAILED", "CANCELLED"}:
        return JobPhase(state)
    if state not in {"PENDING", "RUNNING"}:
        raise InvalidTransition(f"Unknown persisted job state: {state!r}")
    if job["cancel_requested"]:
        return JobPhase.CANCELLING
    if state == "PENDING":
        return JobPhase.DEFERRED if job["not_before"] > now else JobPhase.QUEUED
    if committed or job["stage"] in COMMIT_STAGES:
        import json
        payload = job.get("payload", {})
        payload = json.loads(payload or "{}") if isinstance(payload, str) else payload
        return JobPhase.DRAINING if payload.get("stop_after_commit") else JobPhase.COMMITTING
    return JobPhase.RUNNING


SOURCE = machine("source", "PENDING", {
    "PENDING": {"start": "SYNCING", "snapshot": "OK", "defer": "DEFERRED", "fail": "FAILED",
                "archive": "REMOVED", "purge": "PURGING"},
    "SYNCING": {"snapshot": "OK", "defer": "DEFERRED", "fail": "FAILED", "recover": "PENDING",
                "archive": "REMOVED", "purge": "PURGING"},
    "OK": {"queue": "PENDING", "start": "SYNCING", "defer": "DEFERRED", "fail": "FAILED",
           "archive": "REMOVED", "purge": "PURGING"},
    "DEFERRED": {"queue": "PENDING", "start": "SYNCING", "snapshot": "OK", "fail": "FAILED",
                 "archive": "REMOVED", "purge": "PURGING"},
    "FAILED": {"queue": "PENDING", "start": "SYNCING", "snapshot": "OK", "defer": "DEFERRED",
               "archive": "REMOVED", "purge": "PURGING"},
    "REMOVED": {"restore": "PENDING", "purge": "PURGING"},
    "PURGING": {"fail_purge": "PURGE_FAILED"},
    "PURGE_FAILED": {"retry_purge": "PURGING", "archive": "REMOVED"},
})
SOURCE_INACTIVE = frozenset({"REMOVED", "PURGING", "PURGE_FAILED"})
SOURCE_PURGING = frozenset({"PURGING", "PURGE_FAILED"})

# Health and operation are orthogonal: a metadata failure must not mark playable
# audio unavailable. Direct helpers also support recovery/administrative repair.
TRACK_OPERATION = machine("track_operation", "IDLE", {
    "IDLE": {"queue": "QUEUED", "start_direct": "RUNNING", "review": "NEEDS_APPROVAL",
             "defer": "DEFERRED", "fail": "FAILED", "stop": "STOPPED"},
    "QUEUED": {"claim": "RUNNING", "defer": "DEFERRED", "review": "NEEDS_APPROVAL",
               "complete": "IDLE", "fail": "FAILED", "stop": "STOPPED"},
    "RUNNING": {"recover": "QUEUED", "defer": "DEFERRED", "review": "NEEDS_APPROVAL",
                "complete": "IDLE", "fail": "FAILED", "stop": "STOPPED"},
    "DEFERRED": {"retry": "QUEUED", "resume": "RUNNING", "review": "NEEDS_APPROVAL",
                 "complete": "IDLE", "fail": "FAILED", "stop": "STOPPED"},
    "NEEDS_APPROVAL": {"approve": "QUEUED", "resume": "RUNNING", "defer": "DEFERRED",
                       "complete": "IDLE", "fail": "FAILED", "stop": "STOPPED"},
    "FAILED": {"retry": "QUEUED", "resume": "RUNNING", "review": "NEEDS_APPROVAL",
               "complete": "IDLE", "defer": "DEFERRED", "stop": "STOPPED"},
    "STOPPED": {"retry": "QUEUED", "resume": "RUNNING", "review": "NEEDS_APPROVAL",
                "complete": "IDLE", "fail": "FAILED"},
})
TRACK_HEALTH = machine("track_health", "UNAVAILABLE", {
    "UNAVAILABLE": {"publish": "AVAILABLE", "audit_missing": "MISSING"},
    "AVAILABLE": {"audit_missing": "MISSING", "detach": "UNAVAILABLE"},
    "MISSING": {"repair": "AVAILABLE", "detach": "UNAVAILABLE"},
})
ORIGIN_AVAILABILITY = machine("origin_availability", "UNKNOWN", {
    "UNKNOWN": {"download": "AVAILABLE", "unavailable": "UNAVAILABLE", "defer": "DEFERRED"},
    "AVAILABLE": {"unavailable": "UNAVAILABLE", "probe": "UNKNOWN", "defer": "DEFERRED"},
    "UNAVAILABLE": {"download": "AVAILABLE", "probe": "UNKNOWN", "defer": "DEFERRED"},
    "DEFERRED": {"download": "AVAILABLE", "unavailable": "UNAVAILABLE", "probe": "UNKNOWN"},
})
DOWNLOAD_PERMISSION = machine("download_permission", "ENABLED", {
    "ENABLED": {"disable": "DISABLED"}, "DISABLED": {"enable": "ENABLED"},
})
ASSET = machine("asset", "CURRENT", {
    "CURRENT": {"replace": "REPLACED"}, "REPLACED": {},
})
RECEIPT = machine("receipt", "COMMITTED", {
    "COMMITTED": {"finalize": "FINALIZED"}, "FINALIZED": {},
})
PUBLICATION = machine("publication", "PREPARED", {
    "PREPARED": {"stage": "STAGED", "abort": "ABORTED"},
    "STAGED": {"swap": "PUBLISHED", "abort": "ABORTED"},
    "PUBLISHED": {"commit": "COMMITTED", "restore": "ABORTED"},
    "COMMITTED": {"finalize": "FINALIZED"}, "FINALIZED": {}, "ABORTED": {},
})
ARTWORK_SELECTION = machine("artwork_selection", "auto", {
    "auto": {"pin": "manual"}, "manual": {"automatic": "auto"},
})

CIRCUIT = machine("download_circuit", "CLOSED", {
    "CLOSED": {"threshold": "OPEN"},
    "OPEN": {"deadline": "HALF_OPEN"},
    "HALF_OPEN": {"success": "CLOSED", "blocked": "OPEN"},
})
COOLDOWN = machine("provider_cooldown", "READY", {
    "READY": {"rate_limited": "WAITING"}, "WAITING": {"deadline": "READY"},
})
UPDATE = machine("runtime_update", "IDLE", {
    "IDLE": {"start": "INSTALLING", "rollback": "ROLLED_BACK"},
    "INSTALLING": {"installed": "VALIDATING", "fail": "FAILED", "cancel": "CANCELLED",
                   "recover": "INTERRUPTED"},
    "VALIDATING": {"verified": "ACTIVATING", "fail": "FAILED", "cancel": "CANCELLED",
                   "recover": "INTERRUPTED"},
    "ACTIVATING": {"activated": "OK", "fail": "FAILED", "recover": "INTERRUPTED"},
    "OK": {"start": "INSTALLING", "rollback": "ROLLED_BACK"},
    "FAILED": {"start": "INSTALLING", "rollback": "ROLLED_BACK"},
    "CANCELLED": {"start": "INSTALLING", "rollback": "ROLLED_BACK"},
    "INTERRUPTED": {"start": "INSTALLING", "rollback": "ROLLED_BACK"},
    "ROLLED_BACK": {"start": "INSTALLING", "rollback": "ROLLED_BACK"},
})
OAUTH_ATTEMPT = machine("oauth_attempt", "PENDING", {
    "PENDING": {"consume": "EXCHANGING", "expire": "EXPIRED", "deny": "DENIED", "cancel": "CANCELLED"},
    "EXCHANGING": {"connected": "CONNECTED", "fail": "FAILED"},
    "CONNECTED": {}, "FAILED": {}, "EXPIRED": {}, "DENIED": {}, "CANCELLED": {},
})
AUTH = machine("spotify_credentials", "DISCONNECTED", {
    "DISCONNECTED": {"connect": "CONNECTED"},
    "CONNECTED": {"expire": "EXPIRED", "disconnect": "DISCONNECTED", "reject": "EXPIRED"},
    "EXPIRED": {"refresh": "REFRESHING", "disconnect": "DISCONNECTED"},
    "REFRESHING": {"success": "CONNECTED", "transient_failure": "EXPIRED", "revoke": "DISCONNECTED"},
})
SPOTIFY_RESOLUTION = machine("spotify_resolution", "UNRESOLVED", {
    "UNRESOLVED": {"resolve": "RESOLVING", "cancel": "CANCELLED"},
    "RESOLVING": {"found": "RESOLVED", "defer": "UNRESOLVED", "fail": "FAILED", "cancel": "CANCELLED"},
    "RESOLVED": {"reuse": "WAITING_AUDIO", "download": "IMPORTING", "link": "LINKED", "cancel": "CANCELLED"},
    "WAITING_AUDIO": {"link": "LINKED", "cancel": "CANCELLED", "fail": "FAILED"},
    "IMPORTING": {"link": "LINKED", "review": "WAITING_AUDIO", "fail": "FAILED", "cancel": "CANCELLED"},
    "LINKED": {}, "FAILED": {"retry": "UNRESOLVED"}, "CANCELLED": {},
})

SERVICE = machine("service", "NEW", {
    "NEW": {"start": "STARTING"},
    "STARTING": {"migrated": "RECOVERING", "fail": "FAILED", "stop": "STOPPING"},
    "RECOVERING": {"ready": "RUNNING", "fail": "FAILED", "stop": "STOPPING"},
    "RUNNING": {"fail": "FAILED", "stop": "STOPPING"},
    "FAILED": {"stop": "STOPPING"},
    "STOPPING": {"stopped": "STOPPED"}, "STOPPED": {"start": "STARTING"},
})
CACHE = machine("metadata_cache", "EMPTY", {
    "EMPTY": {"fetch": "FETCHING"},
    "STALE": {"fetch": "FETCHING", "evict": "EMPTY"},
    "FETCHING": {"resolve": "FRESH", "fail_empty": "EMPTY", "fail_stale": "STALE"},
    "FRESH": {"expire": "STALE", "evict": "EMPTY"},
})
SCHEDULE = machine("schedule", "UNSCHEDULED", {
    "UNSCHEDULED": {"arm": "WAITING", "disable": "DISABLED"},
    "WAITING": {"deadline": "DUE", "disable": "DISABLED"},
    "DUE": {"dispatch": "WAITING", "disable": "DISABLED"},
    "DISABLED": {"enable": "UNSCHEDULED"},
})


def cache_state(cached, pending: bool, now: float) -> str:
    if cached and cached[0] > now:
        return "FRESH"
    if pending:
        return "FETCHING"
    return "STALE" if cached else "EMPTY"


def schedule_state(next_at: float | None, now: float, *, enabled: bool = True) -> str:
    if not enabled:
        return "DISABLED"
    if next_at is None:
        return "UNSCHEDULED"
    return "WAITING" if next_at > now else "DUE"


MACHINES = MappingProxyType({m.name: m for m in (
    JOB, JOB_CONTROL, SOURCE, TRACK_OPERATION, TRACK_HEALTH, ORIGIN_AVAILABILITY,
    DOWNLOAD_PERMISSION, ASSET, RECEIPT, PUBLICATION, ARTWORK_SELECTION,
    CIRCUIT, COOLDOWN, UPDATE, OAUTH_ATTEMPT, AUTH, SPOTIFY_RESOLUTION, SERVICE, CACHE, SCHEDULE,
)})
PERSISTED = (
    ("jobs", "state", JOB), ("sources", "status", SOURCE),
    ("tracks", "operation_state", TRACK_OPERATION), ("tracks", "health", TRACK_HEALTH),
    ("track_origins", "availability", ORIGIN_AVAILABILITY), ("assets", "state", ASSET),
    ("artwork_preferences", "mode", ARTWORK_SELECTION),
)


def receipt_state(row: Mapping) -> str:
    return "FINALIZED" if row["finalized_at"] else "COMMITTED"


def cooldown_state(until: float, now: float) -> str:
    return "WAITING" if until > now else "READY"


def circuit_state(record: Mapping, now: float) -> str:
    until = float(record.get("until") or 0)
    if until > now:
        return "OPEN"
    return "HALF_OPEN" if until else "CLOSED"


def credential_state(token: Mapping | None, now: float, *, configured: bool = True) -> str:
    if not configured or not token or not token.get("refresh_token"):
        return "DISCONNECTED"
    return "CONNECTED" if float(token.get("expires_at") or 0) > now + 60 else "EXPIRED"


def resolution_state(payload: Mapping) -> str:
    if payload.get("mode") in {"spotify_link", "spotify_audio"}:
        return "WAITING_AUDIO"
    if payload.get("resolved_audio"):
        return "RESOLVED"
    return "UNRESOLVED"


def guard_schema() -> str:
    """SQLite protects *all* writers, including direct SQL in older services.

    The schema is generated from the very same transition tables used by Python.
    There is no Python user-defined SQL function or plugin required to open a DB.
    """
    sql = ["""CREATE TABLE IF NOT EXISTS state_transition_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        machine TEXT NOT NULL, entity_id TEXT NOT NULL,
        before_state TEXT NOT NULL, after_state TEXT NOT NULL,
        at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    );"""]
    for table, field, spec in PERSISTED:
        name = f"fsm_{table}_{field}"
        states = ",".join(repr(value) for value in spec.transitions)
        pairs = [f"(OLD.{field}={before!r} AND NEW.{field}={after!r})"
                 for before, events in spec.transitions.items() for after in set(events.values())]
        allowed = " OR ".join(pairs) or "0"
        sql.extend([
            f"DROP TRIGGER IF EXISTS {name}_insert;",
            f"""CREATE TRIGGER {name}_insert BEFORE INSERT ON {table}
                WHEN NEW.{field} NOT IN ({states}) OR NEW.{field} IS NULL
                BEGIN SELECT RAISE(ABORT, 'Invalid {spec.name} state'); END;""",
            f"DROP TRIGGER IF EXISTS {name}_update;",
            f"""CREATE TRIGGER {name}_update BEFORE UPDATE OF {field} ON {table}
                WHEN NEW.{field} IS NOT OLD.{field} AND NOT ({allowed})
                BEGIN SELECT RAISE(ABORT, 'Invalid {spec.name} transition'); END;""",
        ])
        # Artwork uses track_id, unlike the ordinary entity tables.
        ident = "track_id" if table == "artwork_preferences" else "id"
        sql.extend([
            f"DROP TRIGGER IF EXISTS {name}_log;",
            f"""CREATE TRIGGER {name}_log AFTER UPDATE OF {field} ON {table}
                WHEN NEW.{field} IS NOT OLD.{field}
                BEGIN INSERT INTO state_transition_log(machine,entity_id,before_state,after_state)
                VALUES ({spec.name!r}, NEW.{ident}, OLD.{field}, NEW.{field}); END;""",
        ])
    stages = ",".join(repr(value) for value in sorted(JOB_STAGES))
    for operation in ("INSERT", "UPDATE OF stage"):
        suffix = "insert" if operation == "INSERT" else "update"
        sql.append(f"""CREATE TRIGGER IF NOT EXISTS fsm_job_stage_{suffix}
            BEFORE {operation} ON jobs WHEN NEW.stage NOT IN ({stages}) OR NEW.stage IS NULL
            BEGIN SELECT RAISE(ABORT, 'Unknown job stage'); END;""")
    for table, field in (("track_origins", "download_enabled"), ("sources", "monitored")):
        for operation in ("INSERT", f"UPDATE OF {field}"):
            suffix = "insert" if operation == "INSERT" else "update"
            sql.append(f"""CREATE TRIGGER IF NOT EXISTS fsm_{table}_{field}_{suffix}
                BEFORE {operation} ON {table} WHEN NEW.{field} NOT IN (0,1) OR NEW.{field} IS NULL
                BEGIN SELECT RAISE(ABORT, 'Invalid permission state'); END;""")
    sql.append("""CREATE TRIGGER IF NOT EXISTS fsm_receipt_finalization
        BEFORE UPDATE OF finalized_at ON operation_receipts
        WHEN OLD.finalized_at IS NOT NULL AND NEW.finalized_at IS NULL
        BEGIN SELECT RAISE(ABORT, 'Finalized receipts cannot be reopened'); END;""")
    return "\n".join(sql)


def validate_database(con) -> None:
    """Fail migration explicitly instead of inventing meanings for unknown states."""
    for table, field, spec in PERSISTED:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            continue
        bad = con.execute(f"SELECT DISTINCT {field} FROM {table}").fetchall()
        unknown = [row[0] for row in bad if row[0] not in spec.transitions]
        if unknown:
            raise InvalidTransition(f"Unknown {table}.{field} values: {unknown!r}; restore or repair a backup")


def transition_setting(store, key: str, spec: Machine, event: str, **data) -> dict:
    """Transition a runtime setting with a DB lock; never persist credentials here."""
    import json
    import time
    with store.db() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        previous = json.loads(row[0]) if row else {}
        state = previous.get("phase") or previous.get("status", spec.initial)
        if spec is UPDATE and state == "RUNNING":
            state = "INSTALLING"  # Legacy runtime update record.
        target = spec.send(state, event)
        result = {**data, "phase": target, "status": "RUNNING" if target in {
            "INSTALLING", "VALIDATING", "ACTIVATING"} else target, "at": time.time()}
        con.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(result)))
        return result


class SwapPhase(StrEnum):
    STAGED = "STAGED"
    PUBLISHED = "PUBLISHED"
    CONFLICT = "CONFLICT"


def swap_phase(actual: str, before: str, after: str) -> SwapPhase:
    """Hash evidence decides whether an idempotent atomic swap is still needed."""
    if actual == after:
        return SwapPhase.PUBLISHED
    if actual == before:
        return SwapPhase.STAGED
    return SwapPhase.CONFLICT


def publication_phase(receipt: Mapping | None, *, result_exists: bool = False,
                      candidate_exists: bool = False) -> str:
    if receipt:
        return receipt_state(receipt)
    if result_exists:
        return "PUBLISHED"
    return "STAGED" if candidate_exists else "PREPARED"


def prepare_update(store, key: str) -> dict:
    previous = store.setting(key, {})
    phase = previous.get("phase", previous.get("status", "IDLE"))
    if phase in {"RUNNING", "INSTALLING", "VALIDATING", "ACTIVATING"}:
        transition_setting(store, key, UPDATE, "recover", error="Interrupted update; retrying")
    return transition_setting(store, key, UPDATE, "start")
