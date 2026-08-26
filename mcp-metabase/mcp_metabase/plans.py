from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from mcp_metabase.models import Action, ExactPlan, Outcome, PlannedMutation
from mcp_metabase.normalization import canonical_sha256


class MetabasePolicyError(RuntimeError):
    """Raised when an exact-action plan violates its stored binding."""


class ExactPlanStore:
    def __init__(
        self,
        *,
        ttl_seconds: int,
        max_plans: int,
        max_plan_bytes: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_plans = max_plans
        self._max_plan_bytes = max_plan_bytes
        self._clock = clock
        self._plans: dict[str, ExactPlan] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _mutation_scope(mutation: PlannedMutation) -> dict[str, Any]:
        return {
            "object_type": mutation.object_type.value,
            "object_id": mutation.object_id,
            "before_sha256": mutation.before_sha256,
            "after_sha256": mutation.after_sha256,
            "write_payload_sha256": canonical_sha256(mutation.write_payload),
            "changed_roots": list(mutation.changed_roots),
            "target": mutation.target,
        }

    @classmethod
    def scope(cls, plan: ExactPlan) -> dict[str, Any]:
        return {
            "plan_id": plan.plan_id,
            "instance": plan.instance,
            "origin": plan.origin,
            "credential_fingerprint": plan.credential_fingerprint,
            "identity_marker": plan.identity_marker,
            "server_version": plan.server_version,
            "action": plan.action.value,
            "mutations": [cls._mutation_scope(item) for item in plan.mutations],
            "arguments_sha256": canonical_sha256(plan.arguments),
            "expires_at_epoch": plan.expires_at,
        }

    def _prune(self) -> None:
        now = self._clock()
        expired = [
            plan_id
            for plan_id, plan in self._plans.items()
            if plan.expires_at <= now or (plan.revoked and plan.outcome is None)
        ]
        for plan_id in expired:
            self._plans.pop(plan_id, None)
        if len(self._plans) < self._max_plans:
            return
        terminal = sorted(
            (plan for plan in self._plans.values() if plan.consumed or plan.revoked),
            key=lambda plan: plan.expires_at,
        )
        while len(self._plans) >= self._max_plans and terminal:
            self._plans.pop(terminal.pop(0).plan_id, None)
        if len(self._plans) >= self._max_plans:
            raise MetabasePolicyError("Metabase exact plan store is full.")

    def prepare(
        self,
        *,
        instance: str,
        origin: str,
        credential_fingerprint: str,
        identity_marker: str,
        server_version: str,
        action: Action,
        mutations: list[PlannedMutation],
        arguments: dict[str, Any],
    ) -> ExactPlan:
        plan = ExactPlan(
            plan_id=str(uuid4()),
            digest="",
            instance=instance,
            origin=origin,
            credential_fingerprint=credential_fingerprint,
            identity_marker=identity_marker,
            server_version=server_version,
            action=action,
            mutations=mutations,
            arguments=arguments,
            expires_at=self._clock() + self._ttl_seconds,
            serialized_bytes=0,
        )
        serialized = json.dumps(
            {
                "scope": self.scope(plan),
                "mutations": [
                    {
                        "before": item.before_state,
                        "after": item.after_state,
                        "payload": item.write_payload,
                    }
                    for item in mutations
                ],
                "arguments": arguments,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        if len(serialized) > self._max_plan_bytes:
            raise MetabasePolicyError("Metabase exact plan exceeds its in-memory byte bound.")
        plan.serialized_bytes = len(serialized)
        plan.digest = canonical_sha256(self.scope(plan))
        with self._lock:
            self._prune()
            self._plans[plan.plan_id] = plan
        return plan

    def _known(self, plan_id: str) -> ExactPlan:
        plan = self._plans.get(plan_id)
        if plan is None:
            raise MetabasePolicyError("Metabase exact plan is unknown or expired.")
        return plan

    def _active(self, plan_id: str) -> ExactPlan:
        plan = self._known(plan_id)
        if plan.revoked:
            raise MetabasePolicyError("Metabase exact plan was revoked.")
        if plan.consumed:
            raise MetabasePolicyError("Metabase exact plan was already consumed.")
        if plan.expires_at <= self._clock():
            plan.revoked = True
            raise MetabasePolicyError("Metabase exact plan expired.")
        return plan

    def peek(self, plan_id: str) -> ExactPlan:
        with self._lock:
            return self._active(plan_id)

    def consume(
        self,
        plan_id: str,
        digest: str,
        *,
        instance: str,
        origin: str,
        credential_fingerprint: str,
        identity_marker: str,
        server_version: str,
        action: Action | None = None,
    ) -> ExactPlan:
        with self._lock:
            plan = self._active(plan_id)
            if (
                plan.digest != digest
                or canonical_sha256(self.scope(plan)) != digest
                or plan.instance != instance
                or plan.origin != origin
                or plan.credential_fingerprint != credential_fingerprint
                or plan.identity_marker != identity_marker
                or plan.server_version != server_version
                or (action is not None and plan.action is not action)
            ):
                raise MetabasePolicyError("Metabase exact plan binding or digest changed.")
            plan.consumed = True
            return plan

    def complete(self, plan_id: str, outcome: Outcome, result: dict[str, Any]) -> ExactPlan:
        with self._lock:
            plan = self._known(plan_id)
            if not plan.consumed:
                raise MetabasePolicyError("Unconsumed Metabase plan cannot be completed.")
            if plan.outcome is not None:
                raise MetabasePolicyError("Metabase exact plan already has a terminal outcome.")
            plan.outcome = outcome
            plan.result = result
            return plan

    def completed(self, plan_id: str) -> ExactPlan:
        with self._lock:
            plan = self._known(plan_id)
            if plan.outcome is None:
                raise MetabasePolicyError("Metabase exact plan has no completed outcome.")
            if plan.expires_at <= self._clock():
                raise MetabasePolicyError("Metabase rollback window expired.")
            return plan

    def revoke(self, plan_id: str, *, identity_marker: str) -> ExactPlan:
        with self._lock:
            plan = self._active(plan_id)
            if plan.identity_marker != identity_marker:
                raise MetabasePolicyError("Metabase exact plan identity binding changed.")
            plan.revoked = True
            return plan
