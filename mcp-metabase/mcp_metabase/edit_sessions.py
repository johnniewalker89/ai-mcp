from __future__ import annotations

import copy
import threading
import time
from collections.abc import Callable
from uuid import uuid4

from mcp_metabase.models import EditSession, ObjectType
from mcp_metabase.plans import MetabasePolicyError


class EditSessionStore:
    """Process-local, fail-closed leases for repeated exact-object work."""

    def __init__(
        self,
        *,
        ttl_seconds: int,
        max_actions: int,
        max_sessions: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_actions = max_actions
        self._max_sessions = max_sessions
        self._clock = clock
        self._sessions: dict[str, EditSession] = {}
        self._lock = threading.Lock()

    @staticmethod
    def snapshot(session: EditSession) -> dict[str, object]:
        bound_objects = []
        for key, state_sha256 in sorted(session.object_state_sha256.items()):
            object_type, raw_object_id = key.split(":", 1)
            bound_objects.append(
                {
                    "object_type": object_type,
                    "object_id": int(raw_object_id),
                    "state_sha256": state_sha256,
                }
            )
        return {
            "session_id": session.session_id,
            "instance": session.instance,
            "origin": session.origin,
            "credential_fingerprint": session.credential_fingerprint,
            "identity_marker": session.identity_marker,
            "server_version": session.server_version,
            "object_type": session.object_type.value,
            "object_id": session.object_id,
            "initial_state_sha256": session.initial_state_sha256,
            "current_state_sha256": session.current_state_sha256,
            "approval_scope": session.approval_scope,
            "bound_object_count": len(bound_objects),
            "bound_objects": bound_objects,
            "expires_at_epoch": session.expires_at,
            "max_actions": session.max_actions,
            "actions_used": session.actions_used,
            "actions_remaining": max(0, session.max_actions - session.actions_used),
            "in_flight": session.in_flight,
            "active": not session.closed,
            "close_reason": session.close_reason,
        }

    @staticmethod
    def _copy(session: EditSession) -> EditSession:
        return copy.deepcopy(session)

    @staticmethod
    def binding_key(object_type: ObjectType, object_id: int) -> str:
        return f"{object_type.value}:{object_id}"

    def _expire(self, session: EditSession) -> None:
        if not session.closed and not session.in_flight and session.expires_at <= self._clock():
            session.closed = True
            session.in_flight = False
            session.close_reason = "expired"

    def _known(self, session_id: str) -> EditSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise MetabasePolicyError("Metabase edit session is unknown or expired.")
        self._expire(session)
        return session

    def _active(self, session_id: str) -> EditSession:
        session = self._known(session_id)
        if session.closed:
            reason = session.close_reason or "closed"
            raise MetabasePolicyError(f"Metabase edit session is closed ({reason}).")
        return session

    def _prune_for_open(self) -> None:
        for session in self._sessions.values():
            self._expire(session)
        terminal = sorted(
            (item for item in self._sessions.values() if item.closed),
            key=lambda item: item.expires_at,
        )
        while len(self._sessions) >= self._max_sessions and terminal:
            self._sessions.pop(terminal.pop(0).session_id, None)
        if len(self._sessions) >= self._max_sessions:
            raise MetabasePolicyError("Metabase edit-session store is full.")

    def open(
        self,
        *,
        instance: str,
        origin: str,
        credential_fingerprint: str,
        identity_marker: str,
        server_version: str,
        object_type: ObjectType,
        object_id: int,
        state_sha256: str,
        object_state_sha256: dict[str, str] | None = None,
        approval_scope: str = "presentation_layout_only",
        ttl_seconds: int | None,
        max_actions: int | None,
    ) -> EditSession:
        requested_ttl = self._ttl_seconds if ttl_seconds is None else ttl_seconds
        requested_actions = self._max_actions if max_actions is None else max_actions
        if (
            type(requested_ttl) is not int
            or requested_ttl <= 0
            or requested_ttl > self._ttl_seconds
        ):
            raise MetabasePolicyError("Metabase edit-session TTL exceeds its configured bound.")
        if (
            type(requested_actions) is not int
            or requested_actions <= 0
            or requested_actions > self._max_actions
        ):
            raise MetabasePolicyError(
                "Metabase edit-session max-actions exceeds its configured bound."
            )
        primary_key = self.binding_key(object_type, object_id)
        bindings = copy.deepcopy(object_state_sha256 or {primary_key: state_sha256})
        if bindings.get(primary_key) != state_sha256:
            raise MetabasePolicyError("Metabase edit-session primary state binding is invalid.")
        if not server_version:
            raise MetabasePolicyError("Metabase edit-session server version binding is missing.")
        with self._lock:
            self._prune_for_open()
            duplicate = any(
                not item.closed
                and item.instance == instance
                and item.origin == origin
                and item.credential_fingerprint == credential_fingerprint
                and item.identity_marker == identity_marker
                and bool(set(item.object_state_sha256) & set(bindings))
                for item in self._sessions.values()
            )
            if duplicate:
                raise MetabasePolicyError(
                    "An active Metabase edit session already owns this exact object."
                )
            session = EditSession(
                session_id=str(uuid4()),
                instance=instance,
                origin=origin,
                credential_fingerprint=credential_fingerprint,
                identity_marker=identity_marker,
                server_version=server_version,
                object_type=object_type,
                object_id=object_id,
                initial_state_sha256=state_sha256,
                current_state_sha256=state_sha256,
                object_state_sha256=bindings,
                approval_scope=approval_scope,
                expires_at=self._clock() + requested_ttl,
                max_actions=requested_actions,
            )
            self._sessions[session.session_id] = session
            return self._copy(session)

    def get(self, session_id: str, *, require_active: bool = False) -> EditSession:
        with self._lock:
            session = self._active(session_id) if require_active else self._known(session_id)
            return self._copy(session)

    def begin_apply(
        self,
        session_id: str,
        *,
        instance: str,
        origin: str,
        credential_fingerprint: str,
        identity_marker: str,
        server_version: str,
    ) -> EditSession:
        with self._lock:
            session = self._active(session_id)
            if (
                session.instance != instance
                or session.origin != origin
                or session.credential_fingerprint != credential_fingerprint
                or session.identity_marker != identity_marker
                or session.server_version != server_version
            ):
                session.closed = True
                session.close_reason = "binding_changed"
                raise MetabasePolicyError("Metabase edit-session binding changed.")
            if session.in_flight:
                raise MetabasePolicyError("Metabase edit session already has an in-flight apply.")
            if session.actions_used >= session.max_actions:
                session.closed = True
                session.close_reason = "max_actions_reached"
                raise MetabasePolicyError("Metabase edit session exhausted its action bound.")
            session.in_flight = True
            return self._copy(session)

    def release_apply(self, session_id: str) -> EditSession:
        with self._lock:
            session = self._active(session_id)
            if not session.in_flight:
                raise MetabasePolicyError("Metabase edit session has no in-flight apply.")
            session.in_flight = False
            return self._copy(session)

    def finish_applied(
        self,
        session_id: str,
        *,
        state_sha256: str | None = None,
        object_state_sha256: dict[str, str] | None = None,
    ) -> EditSession:
        with self._lock:
            session = self._active(session_id)
            if not session.in_flight:
                raise MetabasePolicyError("Metabase edit session has no in-flight apply.")
            if object_state_sha256 is not None:
                primary_key = self.binding_key(session.object_type, session.object_id)
                if primary_key not in object_state_sha256:
                    raise MetabasePolicyError(
                        "Metabase edit-session update lost its primary object binding."
                    )
                overlapping = any(
                    item.session_id != session.session_id
                    and not item.closed
                    and bool(set(item.object_state_sha256) & set(object_state_sha256))
                    for item in self._sessions.values()
                )
                if overlapping:
                    raise MetabasePolicyError(
                        "Metabase edit-session graph overlaps another active session."
                    )
                session.object_state_sha256 = copy.deepcopy(object_state_sha256)
                session.current_state_sha256 = object_state_sha256[primary_key]
            elif state_sha256 is not None:
                primary_key = self.binding_key(session.object_type, session.object_id)
                session.current_state_sha256 = state_sha256
                session.object_state_sha256[primary_key] = state_sha256
            else:
                raise MetabasePolicyError("Metabase edit-session result has no state binding.")
            session.actions_used += 1
            session.in_flight = False
            if session.actions_used >= session.max_actions:
                session.closed = True
                session.close_reason = "max_actions_reached"
            return self._copy(session)

    def finish_query(self, session_id: str) -> EditSession:
        with self._lock:
            session = self._active(session_id)
            if not session.in_flight:
                raise MetabasePolicyError("Metabase edit session has no in-flight action.")
            session.actions_used += 1
            session.in_flight = False
            if session.actions_used >= session.max_actions:
                session.closed = True
                session.close_reason = "max_actions_reached"
            return self._copy(session)

    def ensure_bindings_available(self, session_id: str, binding_keys: set[str]) -> None:
        with self._lock:
            session = self._active(session_id)
            overlapping = any(
                item.session_id != session.session_id
                and not item.closed
                and bool(set(item.object_state_sha256) & binding_keys)
                for item in self._sessions.values()
            )
            if overlapping:
                raise MetabasePolicyError(
                    "Metabase edit-session graph overlaps another active session."
                )

    def reserve_bindings(
        self,
        session_id: str,
        *,
        object_state_sha256: dict[str, str],
    ) -> EditSession:
        """Reserve an exact graph while an action is in flight."""

        with self._lock:
            session = self._active(session_id)
            if not session.in_flight:
                raise MetabasePolicyError("Metabase edit session has no in-flight action.")
            primary_key = self.binding_key(session.object_type, session.object_id)
            if primary_key not in object_state_sha256:
                raise MetabasePolicyError(
                    "Metabase edit-session reservation lost its primary object binding."
                )
            overlapping = any(
                item.session_id != session.session_id
                and not item.closed
                and bool(set(item.object_state_sha256) & set(object_state_sha256))
                for item in self._sessions.values()
            )
            if overlapping:
                raise MetabasePolicyError(
                    "Metabase edit-session graph overlaps another active session."
                )
            session.object_state_sha256 = copy.deepcopy(object_state_sha256)
            session.current_state_sha256 = object_state_sha256[primary_key]
            return self._copy(session)

    def active_for_object(
        self,
        *,
        instance: str,
        origin: str,
        credential_fingerprint: str,
        identity_marker: str,
        object_type: ObjectType,
        object_id: int,
    ) -> EditSession | None:
        key = self.binding_key(object_type, object_id)
        with self._lock:
            for session in self._sessions.values():
                self._expire(session)
                if (
                    not session.closed
                    and session.instance == instance
                    and session.origin == origin
                    and session.credential_fingerprint == credential_fingerprint
                    and session.identity_marker == identity_marker
                    and key in session.object_state_sha256
                ):
                    return self._copy(session)
        return None

    def fail_apply(self, session_id: str, *, reason: str) -> EditSession:
        with self._lock:
            session = self._known(session_id)
            session.in_flight = False
            session.closed = True
            session.close_reason = reason
            return self._copy(session)

    def close(self, session_id: str, *, reason: str = "closed_by_user") -> EditSession:
        with self._lock:
            session = self._known(session_id)
            if session.in_flight:
                raise MetabasePolicyError(
                    "Metabase edit session cannot be closed while an apply is in flight."
                )
            session.in_flight = False
            session.closed = True
            session.close_reason = session.close_reason or reason
            return self._copy(session)
