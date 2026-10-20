"""Offline checks of actual spending and recovery invariants; no website calls."""

import copy
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/transaction_state.py"
SPEC = importlib.util.spec_from_file_location("cucc_transaction_state", SCRIPT)
GUARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GUARD)


def plan(category="data"):
    return {
        "account": "139****0000", "category": category, "product_id": "TEST-PRODUCT",
        "product_name": "测试权益", "contents": ["测试权益"], "quantity": 1,
        "points": 300, "cash": "0.00", "validity": "测试有效期", "renewal": "none",
        "authorization_confirmed": True,
    }


def observed(selected=None):
    value = copy.deepcopy(selected or plan())
    value.pop("authorization_confirmed")
    value.update(stage="checkout", loaded=True, applicable_points=1000,
                 verification_required=True, verification_completed=True)
    return value


def completed(**changes):
    value = {
        "phase": "order_completed", "account": "139****0000", "product_name": "测试权益",
        "order_id": "TEST-ORDER-1", "source": "测试夹具：匹配的官方订单详情",
    }
    value.update(changes)
    return value


class TransactionChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name) / "transaction.json"

    def read(self):
        return json.loads(self.state.read_text())

    def test_three_categories_can_reserve_only_one_submission(self):
        for category in ("data", "voice", "media"):
            with self.subTest(category=category):
                target = self.state.with_name(category + ".json")
                selected = plan(category)
                GUARD.prepare(target, selected)
                GUARD.arm_submit(target, observed(selected))
                with self.assertRaises(GUARD.GuardError):
                    GUARD.arm_submit(target, observed(selected))
                self.assertEqual(json.loads(target.read_text())["phase"], "submitting")

    def test_unselected_transaction_cannot_be_prepared(self):
        selected = plan()
        selected["authorization_confirmed"] = False
        with self.assertRaises(GUARD.GuardError):
            GUARD.prepare(self.state, selected)
        self.assertFalse(self.state.exists())

    def test_every_material_checkout_change_blocks_submission(self):
        GUARD.prepare(self.state, plan())
        changes = {
            "account": "138****1111", "category": "voice", "product_id": "OTHER",
            "product_name": "其他权益", "contents": ["附带另一项业务"], "quantity": 2,
            "points": 301, "cash": "0.01", "validity": "另一有效期", "renewal": "monthly",
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                current = observed()
                current[field] = value
                with self.assertRaises(GUARD.GuardError):
                    GUARD.arm_submit(self.state, current)
                self.assertEqual(self.read()["phase"], "prepared")

    def test_unloaded_insufficient_or_unverified_checkout_cannot_submit(self):
        GUARD.prepare(self.state, plan())
        for field, value in (("loaded", False), ("applicable_points", 299),
                             ("verification_completed", False), ("verification_required", None)):
            with self.subTest(field=field):
                current = observed()
                current[field] = value
                with self.assertRaises(GUARD.GuardError):
                    GUARD.arm_submit(self.state, current)
        self.assertEqual(self.read()["phase"], "prepared")

    def test_user_already_completed_is_reconciled_without_new_submit(self):
        GUARD.prepare(self.state, plan())
        current = observed()
        current["stage"] = "paid"
        with self.assertRaises(GUARD.GuardError) as caught:
            GUARD.arm_submit(self.state, current)
        self.assertEqual(caught.exception.action, "reconcile")
        GUARD.record_result(self.state, completed())
        with self.assertRaises(GUARD.GuardError):
            GUARD.arm_submit(self.state, observed())

    def test_old_observation_does_not_reopen_a_completed_order(self):
        GUARD.prepare(self.state, plan())
        GUARD.record_result(self.state, completed())
        GUARD.record_result(self.state, {"phase": "paid", "source": "旧的支付页面"})
        self.assertEqual(self.read()["phase"], "order_completed")
        with self.assertRaises(GUARD.GuardError):
            GUARD.record_result(self.state, {"phase": "awaiting_verification", "source": "旧结算页"})

    def test_existing_order_cannot_be_replaced_or_mismatched(self):
        GUARD.prepare(self.state, plan())
        GUARD.record_result(self.state, completed())
        for change in ({"order_id": "OTHER-ORDER"}, {"account": "138****1111"}, {"product_name": "其他权益"}):
            with self.subTest(change=change), self.assertRaises(GUARD.GuardError):
                GUARD.record_result(self.state, completed(**change))
        self.assertEqual(self.read()["order_id"], "TEST-ORDER-1")

    def test_order_completion_does_not_invent_spend_balance_or_benefit_proof(self):
        GUARD.prepare(self.state, plan())
        GUARD.record_result(self.state, completed())
        evidence = self.read()["results"][-1]
        self.assertNotIn("points_spent", evidence)
        self.assertNotIn("communication_balance", evidence)
        with self.assertRaises(GUARD.GuardError):
            GUARD.record_result(self.state, completed(phase="benefit_verified"))
        GUARD.record_result(self.state, completed(phase="benefit_verified", benefit_source="测试夹具：本次运营商成功回执"))
        self.assertEqual(self.read()["phase"], "benefit_verified")

    def test_cost_mismatch_is_retained_until_explicit_matching_cost_evidence(self):
        GUARD.prepare(self.state, plan())
        GUARD.record_result(self.state, completed(points_spent=301, cash_paid="0.01"))
        self.assertEqual(self.read()["phase"], "needs_reconcile")
        GUARD.record_result(self.state, completed())
        self.assertEqual(self.read()["phase"], "needs_reconcile")
        GUARD.record_result(self.state, completed(points_spent=300, cash_paid="0.00", source="更新后的费用核对证据"))
        self.assertEqual(self.read()["phase"], "order_completed")

    def test_pending_verification_can_record_zero_spend(self):
        GUARD.prepare(self.state, plan())
        GUARD.record_result(self.state, {"phase": "awaiting_verification", "points_spent": 0, "source": "未扣积分的验证页"})
        self.assertEqual(self.read()["phase"], "awaiting_verification")
        GUARD.arm_submit(self.state, observed())

    def test_invalid_money_boolean_points_and_unmasked_account_are_rejected(self):
        changes = [("cash", "NaN"), ("cash", "Infinity"), ("cash", "1e1000"),
                   ("cash", "-0.01"), ("points", True), ("account", "13900000000"), ("category", [])]
        for field, value in changes:
            with self.subTest(field=field, value=value):
                selected = plan()
                selected[field] = value
                with self.assertRaises(GUARD.GuardError):
                    GUARD.prepare(self.state, selected)
        self.assertFalse(self.state.exists())

    def test_extra_secret_fields_are_not_persisted(self):
        selected = plan()
        selected["password"] = "not-a-real-password"
        with self.assertRaises(GUARD.GuardError):
            GUARD.prepare(self.state, selected)
        GUARD.prepare(self.state, plan())
        current = observed()
        current["otp"] = "not-a-real-code"
        with self.assertRaises(GUARD.GuardError):
            GUARD.arm_submit(self.state, current)
        result = completed(cookie="not-a-real-cookie")
        with self.assertRaises(GUARD.GuardError):
            GUARD.record_result(self.state, result)
        self.assertNotIn("not-a-real", self.state.read_text())

    def test_existing_plan_cannot_be_overwritten_and_files_are_private(self):
        GUARD.prepare(self.state, plan())
        with self.assertRaises(GUARD.GuardError):
            GUARD.prepare(self.state, plan())
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(Path(str(self.state) + ".lock").stat().st_mode), 0o600)

    def test_two_processes_cannot_both_reserve_submission(self):
        GUARD.prepare(self.state, plan())
        current = self.state.with_name("observed.json")
        current.write_text(json.dumps(observed()))
        command = [sys.executable, str(SCRIPT), "--state", str(self.state), "arm-submit", "--observed", str(current)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: subprocess.run(command, capture_output=True, text=True), range(2)))
        self.assertEqual(sorted(result.returncode for result in results), [0, 2])
        self.assertEqual(sum(json.loads(result.stdout).get("action") == "submit_allowed" for result in results), 1)
        self.assertEqual(self.read()["phase"], "submitting")


if __name__ == "__main__":
    unittest.main()
