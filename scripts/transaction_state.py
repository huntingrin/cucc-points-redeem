#!/usr/bin/env python3
"""Local-only redemption plan, one-attempt guard, and reconciliation record."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


PLAN_FIELDS = {
    "account", "category", "product_id", "product_name", "contents", "quantity",
    "points", "cash", "validity", "renewal", "authorization_confirmed",
}
OBSERVATION_FIELDS = (PLAN_FIELDS - {"authorization_confirmed"}) | {
    "stage", "loaded", "applicable_points", "verification_required",
    "verification_completed",
}
RESULT_FIELDS = {
    "phase", "account", "product_id", "product_name", "order_id", "points_spent",
    "cash_paid", "communication_balance", "reward_balance", "source", "benefit_source",
}
RANK = {
    "prepared": 0, "awaiting_verification": 1, "submitting": 2, "paid": 3,
    "processing": 4, "order_completed": 5, "benefit_verified": 6,
}
RESULT_PHASES = {
    "awaiting_verification", "paid", "processing", "order_completed",
    "benefit_verified", "needs_reconcile",
}


class GuardError(ValueError):
    def __init__(self, message, action="inspect"):
        super().__init__(message)
        self.action = action


def now():
    return datetime.now(timezone.utc).isoformat()


def read_object(path):
    with Path(path).open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise GuardError("JSON input must be an object.")
    return value


def allowed_fields(value, allowed):
    if not isinstance(value, dict):
        raise GuardError("Input must be a JSON object.")
    if set(value) - allowed:
        raise GuardError("Unexpected fields; use only the documented schema.")


def text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise GuardError(f"{field} must be a nonempty string.")
    return " ".join(value.split())


def integer(value, field, minimum=0):
    if type(value) is not int or value < minimum:
        raise GuardError(f"{field} must be an integer >= {minimum}.")
    return value


def money(value, field):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise GuardError(f"{field} must be a finite nonnegative decimal.")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise GuardError(f"{field} must be a decimal.") from exc
    if not amount.is_finite() or amount < 0:
        raise GuardError(f"{field} must be finite and nonnegative.")
    if amount.as_tuple().exponent < -2:
        raise GuardError(f"{field} must have at most two decimal places.")
    try:
        return format(amount.quantize(Decimal("0.01")), ".2f")
    except InvalidOperation as exc:
        raise GuardError(f"{field} exceeds supported decimal precision.") from exc


def normalize_plan(value, require_authorization=True):
    allowed_fields(value, PLAN_FIELDS)
    missing = PLAN_FIELDS - set(value)
    if missing:
        raise GuardError("Plan is incomplete.")
    account = text(value["account"], "account")
    if not re.fullmatch(r"1[3-9]\d\*{4}\d{4}", account):
        raise GuardError("account must use a masked mobile number such as 139****0000.")
    category = text(value["category"], "category")
    if category not in {"data", "voice", "media"}:
        raise GuardError("category must be data, voice, or media.")
    if value["renewal"] != "none":
        raise GuardError("This workflow requires verified no automatic renewal.")
    if type(value["authorization_confirmed"]) is not bool:
        raise GuardError("authorization_confirmed must be boolean.")
    if require_authorization and value["authorization_confirmed"] is not True:
        raise GuardError("This transaction has not been authorized.", "await_selection")
    if not isinstance(value["contents"], list) or not value["contents"]:
        raise GuardError("contents must list the product and all bundled benefits.")
    contents = [text(item, "contents") for item in value["contents"]]
    cash = money(value["cash"], "cash")
    if cash != "0.00":
        raise GuardError("Cash top-up is outside this workflow.", "revise_plan")
    return {
        "account": account,
        "category": category,
        "product_id": text(value["product_id"], "product_id"),
        "product_name": text(value["product_name"], "product_name"),
        "contents": sorted(contents),
        "quantity": integer(value["quantity"], "quantity", 1),
        "points": integer(value["points"], "points", 1),
        "cash": cash,
        "validity": text(value["validity"], "validity"),
        "renewal": "none",
        "authorization_confirmed": value["authorization_confirmed"],
    }


def fingerprint(plan):
    data = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


@contextmanager
def locked(state_path):
    state_path = Path(state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(str(state_path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise GuardError("Another operation holds this transaction lock.", "inspect") from exc
        yield state_path
    finally:
        os.close(descriptor)


def write_state(path, state):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix="." + path.name + ".", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            json.dump(state, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_state(path):
    state = read_object(path)
    if state.get("schema") != 1 or state.get("phase") not in set(RANK) | {"needs_reconcile"}:
        raise GuardError("Unknown transaction state; inspect the existing record.", "reconcile")
    plan = normalize_plan(state.get("plan", {}))
    if state.get("plan_hash") != fingerprint(plan):
        raise GuardError("Stored plan no longer matches its fingerprint.", "reconcile")
    return state


def prepare(state_path, plan):
    normalized = normalize_plan(plan)
    with locked(state_path) as path:
        if path.exists():
            raise GuardError("Transaction record already exists; inspect it instead of resetting.", "reconcile")
        state = {
            "schema": 1, "phase": "prepared", "plan": normalized,
            "plan_hash": fingerprint(normalized), "created_at": now(), "results": [],
        }
        write_state(path, state)
    return {"ok": True, "action": "prepare_checkout", "phase": "prepared"}


def arm_submit(state_path, observed):
    allowed_fields(observed, OBSERVATION_FIELDS)
    with locked(state_path) as path:
        state = load_state(path)
        if state["phase"] not in {"prepared", "awaiting_verification"}:
            raise GuardError("A submission/result already exists or is uncertain; reconcile first.", "reconcile")
        if observed.get("stage") != "checkout":
            raise GuardError("The page is not the same pending checkout; inspect its result.", "reconcile")
        if observed.get("loaded") is not True:
            raise GuardError("Business data has not finished loading.", "wait_for_data")
        candidate = {key: observed[key] for key in PLAN_FIELDS - {"authorization_confirmed"} if key in observed}
        candidate["authorization_confirmed"] = True
        current = normalize_plan(candidate)
        if current != state["plan"]:
            raise GuardError("Checkout differs from the selected transaction.", "revise_plan")
        balance = integer(observed.get("applicable_points"), "applicable_points")
        if balance < current["points"]:
            raise GuardError("Applicable points are insufficient.", "revise_plan")
        for field in ("verification_required", "verification_completed"):
            if type(observed.get(field)) is not bool:
                raise GuardError(f"{field} must be explicitly known.", "inspect")
        if observed["verification_required"] and not observed["verification_completed"]:
            raise GuardError("Website verification still needs the user.", "await_verification")
        state["phase"] = "submitting"
        state["submit_intent_at"] = now()
        write_state(path, state)
    return {"ok": True, "action": "submit_allowed", "phase": "submitting"}


def record_result(state_path, result):
    allowed_fields(result, RESULT_FIELDS)
    phase = result.get("phase")
    if phase not in RESULT_PHASES:
        raise GuardError("Unknown result phase.")
    clean = {"phase": phase, "source": text(result.get("source"), "source"), "at": now()}
    with locked(state_path) as path:
        state = load_state(path)
        plan = state["plan"]
        for field in ("account", "product_id", "product_name"):
            if field in result:
                actual = text(result[field], field)
                if actual != plan[field]:
                    raise GuardError(f"Result {field} does not match this transaction.", "reconcile")
                clean[field] = actual
        order_id = result.get("order_id", state.get("order_id"))
        if order_id is not None:
            order_id = text(order_id, "order_id")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", order_id):
                raise GuardError("Invalid order_id.")
            if state.get("order_id") and state["order_id"] != order_id:
                raise GuardError("A different order cannot replace this transaction's order.", "reconcile")
            clean["order_id"] = order_id
        if phase in {"processing", "order_completed", "benefit_verified"} and not order_id:
            raise GuardError("This result phase requires the matching order number.", "reconcile")
        if phase in {"order_completed", "benefit_verified"}:
            if not all(field in clean for field in ("account", "product_name")):
                raise GuardError("Completed evidence must match account and product_name.", "reconcile")
        if phase == "benefit_verified":
            clean["benefit_source"] = text(result.get("benefit_source"), "benefit_source")
        for field in ("points_spent", "communication_balance", "reward_balance"):
            if field in result:
                clean[field] = integer(result[field], field)
        if "cash_paid" in result:
            clean["cash_paid"] = money(result["cash_paid"], "cash_paid")
        if phase == "awaiting_verification" and state["phase"] not in {"prepared", "awaiting_verification"}:
            raise GuardError("An attempted submission cannot return to a submit-ready state.", "reconcile")
        mismatches = set(state.get("cost_mismatches", []))
        expected = {"points_spent": 0 if phase == "awaiting_verification" else plan["points"], "cash_paid": plan["cash"]}
        for field, value in expected.items():
            if field in clean:
                if clean[field] == value:
                    mismatches.discard(field)
                else:
                    mismatches.add(field)
        if mismatches:
            clean["mismatches"] = sorted(mismatches)
            next_phase = "needs_reconcile"
        elif phase == "needs_reconcile" or state["phase"] == "needs_reconcile":
            next_phase = phase if phase in {"order_completed", "benefit_verified"} else "needs_reconcile"
        else:
            next_phase = max((state["phase"], phase), key=lambda key: RANK[key])
        state["phase"] = next_phase
        state["cost_mismatches"] = sorted(mismatches)
        if order_id:
            state["order_id"] = order_id
        state["results"].append(clean)
        state["updated_at"] = now()
        write_state(path, state)
    return {"ok": True, "action": "recorded", "phase": next_phase}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare").add_argument("--plan", type=Path, required=True)
    commands.add_parser("arm-submit").add_argument("--observed", type=Path, required=True)
    commands.add_parser("record").add_argument("--result", type=Path, required=True)
    commands.add_parser("status")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            output = prepare(args.state, read_object(args.plan))
        elif args.command == "arm-submit":
            output = arm_submit(args.state, read_object(args.observed))
        elif args.command == "record":
            output = record_result(args.state, read_object(args.result))
        else:
            with locked(args.state) as path:
                output = {"ok": True, "state": load_state(path)}
        print(json.dumps(output, ensure_ascii=False, allow_nan=False))
        return 0
    except (GuardError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "action": getattr(exc, "action", "inspect"), "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
