"""
tests/unit/test_plan_scope.py — Unit tests for the Terraform plan-scope guard
(execute.assert_plan_scope) that gates every instance_failure remediation.

The invariant under test, stated positively: after inert entries are
discarded, the plan must contain exactly one create, at
module.compute.aws_instance.targets["<failed-target>"]; no deletes and no
replacements, whatever they are keyed to; and any further change must be an
update whose for_each key is that same target.

That last clause is load-bearing and was missing from the first revision of
this suite. modules/compute declares both aws_cloudwatch_metric_alarm.cpu_high
and .status_check_failed with `dimensions = { InstanceId =
aws_instance.targets[each.key].id }`, so recreating an instance always makes
its two alarms' dimensions "known after apply" and Terraform always plans an
update for each. A genuine recreation is a three-change plan. The earlier
"exactly one resource change" rule would have rejected every real
instance_failure remediation before apply — and this suite could not have
caught it, because it asserted the same wrong rule the implementation did.
test_real_recreation_shape_is_accepted below is the case that was missing.

Attribution by for_each key rather than absolute address is the rule Gate 4
already uses (plan.drift_validation), for exactly this reason. The two gates
share that rule while still answering different questions: Gate 4 asks whether
state reflects reality, this asks what mutations are about to be applied. They
are deliberately not merged; test_drift_validation.py covers the other one.

assert_plan_scope is pure — it takes parsed plan JSON — so every branch below
runs with no AWS account and no terraform binary.

Thesis reference: §3.5.3, §3.5.4, §3.7.2, §4.4.3, Table 4.8, Table 4.10
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ORCHESTRATOR = Path(__file__).resolve().parent.parent.parent / "orchestrator"
sys.path.insert(0, str(_ORCHESTRATOR))

from execute import UnsafePlanScopeError, assert_plan_scope

TARGET = "target-2"
EXPECTED_ADDR = 'module.compute.aws_instance.targets["target-2"]'

# The two dependants that make a real recreation a three-change plan. Both are
# declared for_each over the same target names as the instance itself, and both
# reference aws_instance.targets[each.key].id in their dimensions.
ALARM_CPU = "module.compute.aws_cloudwatch_metric_alarm.cpu_high"
ALARM_STATUS = "module.compute.aws_cloudwatch_metric_alarm.status_check_failed"


def _change(address: str, actions: list[str]) -> dict:
    return {"address": address, "change": {"actions": actions}}


def _plan(*changes: dict) -> dict:
    return {"format_version": "1.2", "resource_changes": list(changes)}


# ── The accepted case ─────────────────────────────────────────────────────────

class TestExpectedPlan:
    def test_single_create_of_the_failed_target_is_accepted(self):
        """The only shape a remediation is authorised to apply."""
        assert_plan_scope(_plan(_change(EXPECTED_ADDR, ["create"])), target=TARGET)

    def test_inert_entries_do_not_break_the_count(self):
        """no-op and read entries mutate nothing and are discarded first."""
        plan = _plan(
            _change('module.compute.aws_instance.targets["target-1"]', ["no-op"]),
            _change('module.compute.aws_instance.targets["target-3"]', ["no-op"]),
            _change("data.aws_ami.ubuntu", ["read"]),
            _change(EXPECTED_ADDR, ["create"]),
        )
        assert_plan_scope(plan, target=TARGET)

    def test_each_target_is_validated_against_its_own_address(self):
        for name in ("target-1", "target-2", "target-3"):
            addr = f'module.compute.aws_instance.targets["{name}"]'
            assert_plan_scope(_plan(_change(addr, ["create"])), target=name)

    def test_real_recreation_shape_is_accepted(self):
        """
        The shape a genuine instance_failure recreation actually produces.

        Both alarms in modules/compute carry
        `dimensions = { InstanceId = aws_instance.targets[each.key].id }`, so
        recreating the instance makes each alarm's dimensions "known after
        apply" and Terraform plans an update for both. Three active changes,
        every time — not one. This case is the reason the guard attributes by
        for_each key instead of counting changes.
        """
        plan = _plan(
            _change(EXPECTED_ADDR, ["create"]),
            _change(f'{ALARM_CPU}["target-2"]', ["update"]),
            _change(f'{ALARM_STATUS}["target-2"]', ["update"]),
        )
        assert_plan_scope(plan, target=TARGET)

    def test_key_is_read_from_the_index_field_when_present(self):
        """
        terraform show -json carries the for_each key in `index`. Prefer it,
        and fall back to parsing the address only when it is absent — the same
        precedence plan.drift_validation() uses for `resource_key`.
        """
        entry = _change(f'{ALARM_CPU}["target-2"]', ["update"])
        entry["index"] = "target-2"
        assert_plan_scope(_plan(_change(EXPECTED_ADDR, ["create"]), entry), target=TARGET)

    def test_many_keyed_updates_are_accepted(self):
        """The rule is attribution, not a count — a fourth keyed dependant is fine."""
        plan = _plan(
            _change(EXPECTED_ADDR, ["create"]),
            _change(f'{ALARM_CPU}["target-2"]', ["update"]),
            _change(f'{ALARM_STATUS}["target-2"]', ["update"]),
            _change('module.compute.aws_eip_association.targets["target-2"]', ["update"]),
        )
        assert_plan_scope(plan, target=TARGET)


# ── The rejected cases ────────────────────────────────────────────────────────

class TestRejectedPlans:
    def test_delete_is_rejected(self):
        """The failure this guard exists for: a plan that destroys something."""
        plan = _plan(
            _change(EXPECTED_ADDR, ["create"]),
            _change(f'{ALARM_CPU}["target-1"]', ["delete"]),
        )
        with pytest.raises(UnsafePlanScopeError) as exc:
            assert_plan_scope(plan, target=TARGET)
        assert "['delete']" in str(exc.value)
        assert "delete" in exc.value.detail

    def test_delete_keyed_to_the_failed_target_is_still_rejected(self):
        """
        Key attribution relaxes updates only. A delete is inadmissible whatever
        it is keyed to — otherwise a plan that removed the target's own alarms
        would be waved through as "keyed to the target".
        """
        plan = _plan(
            _change(EXPECTED_ADDR, ["create"]),
            _change(f'{ALARM_CPU}["target-2"]', ["delete"]),
        )
        with pytest.raises(UnsafePlanScopeError) as exc:
            assert_plan_scope(plan, target=TARGET)
        assert "['delete']" in str(exc.value)

    def test_update_keyed_to_another_target_is_rejected(self):
        """The same alarm resource, wrong for_each key — a neighbour's alarm."""
        plan = _plan(
            _change(EXPECTED_ADDR, ["create"]),
            _change(f'{ALARM_CPU}["target-3"]', ["update"]),
        )
        with pytest.raises(UnsafePlanScopeError) as exc:
            assert_plan_scope(plan, target=TARGET)
        assert "target-3" in str(exc.value)
        assert "target-2" in str(exc.value)

    def test_unkeyed_update_is_rejected(self):
        """
        A shared, unkeyed resource carries no for_each key, so it can never be
        attributed to the failed target. Indeterminate attribution fails closed.
        """
        plan = _plan(
            _change(EXPECTED_ADDR, ["create"]),
            _change("module.networking.aws_security_group.targets", ["update"]),
        )
        with pytest.raises(UnsafePlanScopeError) as exc:
            assert_plan_scope(plan, target=TARGET)
        assert "aws_security_group.targets" in str(exc.value)

    def test_replace_of_a_live_instance_is_rejected(self):
        """
        ["delete", "create"] means the instance still exists and Terraform
        chose to replace it — more destructive than the create this
        remediation is authorised to perform, and not what a genuinely
        terminated instance produces.
        """
        plan = _plan(_change(EXPECTED_ADDR, ["delete", "create"]))
        with pytest.raises(UnsafePlanScopeError) as exc:
            assert_plan_scope(plan, target=TARGET)
        assert "['delete', 'create']" in str(exc.value)

    def test_create_before_destroy_replace_is_also_rejected(self):
        plan = _plan(_change(EXPECTED_ADDR, ["create", "delete"]))
        with pytest.raises(UnsafePlanScopeError):
            assert_plan_scope(plan, target=TARGET)

    def test_unexpected_second_create_is_rejected(self):
        """Two creates — e.g. a resurrected ASG — is a scope violation."""
        plan = _plan(
            _change(EXPECTED_ADDR, ["create"]),
            _change("module.asg_baseline.aws_autoscaling_group.baseline", ["create"]),
        )
        with pytest.raises(UnsafePlanScopeError) as exc:
            assert_plan_scope(plan, target=TARGET)
        assert "2 creates" in str(exc.value)

    def test_wrong_target_address_is_rejected(self):
        """One create, right resource type, wrong for_each key."""
        wrong = 'module.compute.aws_instance.targets["target-3"]'
        with pytest.raises(UnsafePlanScopeError) as exc:
            assert_plan_scope(_plan(_change(wrong, ["create"])), target=TARGET)
        assert "target-3" in str(exc.value)
        assert EXPECTED_ADDR in str(exc.value)

    def test_unexpected_update_is_rejected(self):
        """An update is a mutation and must not ride along with the create."""
        plan = _plan(
            _change(EXPECTED_ADDR, ["create"]),
            _change("module.compute.aws_instance.targets[\"target-1\"]", ["update"]),
        )
        with pytest.raises(UnsafePlanScopeError):
            assert_plan_scope(plan, target=TARGET)

    def test_lone_update_of_the_target_is_rejected(self):
        """
        An update without a create means the instance still exists, so the
        alert cannot be resolved by recreating it — the same reasoning that
        rejects an empty plan.
        """
        with pytest.raises(UnsafePlanScopeError) as exc:
            assert_plan_scope(_plan(_change(EXPECTED_ADDR, ["update"])), target=TARGET)
        assert "0 creates" in str(exc.value)

    def test_empty_plan_is_rejected(self):
        """
        Nothing to do means the for_each key is present, so the alert cannot be
        resolved by an apply. Applying nothing and reporting success would
        record a trial whose T_remediate measured no remediation at all.
        """
        with pytest.raises(UnsafePlanScopeError) as exc:
            assert_plan_scope(_plan(), target=TARGET)
        assert "no changes" in str(exc.value)

    def test_plan_of_only_inert_entries_is_rejected(self):
        plan = _plan(
            _change('module.compute.aws_instance.targets["target-1"]', ["no-op"]),
            _change("data.aws_ami.ubuntu", ["read"]),
        )
        with pytest.raises(UnsafePlanScopeError) as exc:
            assert_plan_scope(plan, target=TARGET)
        assert "no changes" in str(exc.value)

    def test_missing_resource_changes_key_is_rejected(self):
        """A malformed or truncated plan must fail closed, not open."""
        with pytest.raises(UnsafePlanScopeError):
            assert_plan_scope({"format_version": "1.2"}, target=TARGET)

    def test_null_resource_changes_is_rejected(self):
        with pytest.raises(UnsafePlanScopeError):
            assert_plan_scope({"resource_changes": None}, target=TARGET)


# ── Contract the caller relies on ─────────────────────────────────────────────

class TestErrorContract:
    def test_error_is_a_runtimeerror_subclass(self):
        """
        _execute_with_retry catches Exception broadly; app.py re-raises this
        type ahead of that catch. It stays a RuntimeError so any caller that
        has not been taught about it still treats it as a dispatch failure
        rather than crashing the worker.
        """
        assert issubclass(UnsafePlanScopeError, RuntimeError)

    def test_detail_lists_every_offending_change(self):
        plan = _plan(
            _change("aws_s3_bucket.evidence", ["delete"]),
            _change(EXPECTED_ADDR, ["create"]),
        )
        with pytest.raises(UnsafePlanScopeError) as exc:
            assert_plan_scope(plan, target=TARGET)
        assert "aws_s3_bucket.evidence" in exc.value.detail
        assert EXPECTED_ADDR in exc.value.detail

    def test_expected_address_is_always_named_in_the_message(self):
        """Operators read this in an SNS escalation; it must say what was wanted."""
        with pytest.raises(UnsafePlanScopeError) as exc:
            assert_plan_scope(_plan(), target=TARGET)
        assert EXPECTED_ADDR in str(exc.value)
