"""Unit tests for workflow schemas, header normalization, and dispositions catalogue."""

from solution.schemas import load_dispositions, load_schemas, normalize_header


def test_load_workflow_schemas():
    """Verify loading official motor_renewal and policy_feedback schemas."""
    schemas = load_schemas()
    assert "motor_renewal" in schemas
    assert "policy_feedback" in schemas

    # Check motor_renewal
    motor = schemas["motor_renewal"]
    field_names = [f.name for f in motor.fields]
    assert field_names == [
        "customer_name",
        "phone",
        "policy_number",
        "policy_expiry",
        "premium_amount",
        "preferred_language",
    ], "Schema field order must be strictly preserved"
    assert motor.reference_field_name == "policy_number"

    # Check policy_feedback
    feedback = schemas["policy_feedback"]
    fb_field_names = [f.name for f in feedback.fields]
    assert fb_field_names == [
        "customer_name",
        "phone",
        "order_id",
        "product",
        "order_date",
        "preferred_language",
    ], "Schema field order must be strictly preserved"
    assert feedback.reference_field_name == "order_id"


def test_header_normalization_and_alias_mapping():
    """Verify header normalization with underscores, whitespace, and case variations."""
    schemas = load_schemas()
    motor = schemas["motor_renewal"]
    header_map = motor.build_header_map()

    # Exact name matches
    assert header_map[normalize_header("customer_name")] == "customer_name"
    assert header_map[normalize_header("phone")] == "phone"

    # Aliases with spaces and underscores
    assert header_map[normalize_header("Mobile No")] == "phone"
    assert header_map[normalize_header("mobile_no")] == "phone"
    assert header_map[normalize_header("  MOBILE___NO  ")] == "phone"
    assert header_map[normalize_header("Insured Name")] == "customer_name"
    assert header_map[normalize_header("Policy No")] == "policy_number"
    assert header_map[normalize_header("Premium")] == "premium_amount"
    assert header_map[normalize_header("Language")] == "preferred_language"

    # Unmatched headers must not be in the mapping
    assert normalize_header("agent_notes") not in header_map


def test_load_dispositions():
    """Verify loading dispositions catalogue from CSV with terminal and success flags."""
    disps = load_dispositions()
    assert "motor_renewal" in disps
    assert "policy_feedback" in disps

    # motor_renewal: renewed is terminal and success
    renewed = disps["motor_renewal"]["renewed"]
    assert renewed.is_terminal is True
    assert renewed.is_success is True

    # motor_renewal: callback_requested is not terminal and not success
    cb = disps["motor_renewal"]["callback_requested"]
    assert cb.is_terminal is False
    assert cb.is_success is False

    # motor_renewal: not_interested is terminal but not success
    ni = disps["motor_renewal"]["not_interested"]
    assert ni.is_terminal is True
    assert ni.is_success is False

    # policy_feedback: feedback_captured is terminal and success
    fc = disps["policy_feedback"]["feedback_captured"]
    assert fc.is_terminal is True
    assert fc.is_success is True
