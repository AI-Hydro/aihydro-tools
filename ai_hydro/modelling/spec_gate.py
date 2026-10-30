"""The single gate a training request passes before any job is spawned.

``propose_and_train`` (a JSON ``ModelSpec``) and ``train_hydro_model`` (flat parameters) both
reduce their input to one ``aihydro_modelling.ModelSpec`` and go through :func:`gate_spec`:
unknown keys are refused (a typo is not silently defaulted), the static pre-flight validation
runs (train/validate/test periods disjoint, ranges), and the backend must be able to run on
this host. The runner re-checks the record itself with :func:`require_trainable` once the
data is resolved, so a window the record does not cover refuses instead of training on nothing.
"""
from __future__ import annotations

from typing import Any

# framework strings the flat tool has always accepted (normalised: lower-case, no - or _)
_HBV_FRAMEWORKS = {"hbv", "hbvlight", "differentiable", "hydrodl2"}
_NH_FRAMEWORKS = {"neuralhydrology", "nh", "lstm"}
# NeuralHydrology architecture string -> ModelSpec backend
_NH_BACKEND = {
    "cudalstm": "nh_lstm", "ealstm": "nh_ealstm", "transformer": "nh_transformer",
    "mclstm": "nh_mclstm", "mamba": "nh_mamba",
}


def _err(code: str, message: str, **extra: Any) -> dict:
    return {"error": True, "code": code, "message": message, **extra}


def flat_to_spec_dict(framework: str, model: str, **fields: Any) -> tuple[dict | None, dict | None]:
    """Reduce ``train_hydro_model``'s flat arguments to ``ModelSpec`` input, or an error dict.

    ``None`` values are dropped so the spec resolves them from the backend (no HBV constants
    are ever passed to a neural backend).
    """
    import difflib

    fw = (framework or "hbv").lower().replace("-", "").replace("_", "")
    if fw in _HBV_FRAMEWORKS:
        backend = "hbv"
    elif fw in _NH_FRAMEWORKS:
        arch = (model or "cudalstm").lower()
        if arch not in _NH_BACKEND:
            return None, _err(
                "INVALID_SPEC",
                f"Unknown neural model {model!r}. Valid: {sorted(_NH_BACKEND)}.",
                did_you_mean=difflib.get_close_matches(arch, list(_NH_BACKEND), n=3),
                hint="Call describe_model_space() to see valid fields and backends.",
            )
        backend = _NH_BACKEND[arch]
    else:
        valid = sorted(_HBV_FRAMEWORKS | _NH_FRAMEWORKS)
        return None, _err(
            "INVALID_SPEC",
            f"Unknown framework {framework!r}. Valid: {valid}.",
            did_you_mean=difflib.get_close_matches(fw, valid, n=3),
            hint="Call describe_model_space() to see valid fields and backends.",
        )
    out = {"backend": backend, **{k: v for k, v in fields.items() if v is not None}}
    return out, None


def gate_spec(raw: dict) -> tuple[Any, dict | None]:
    """Parse + statically validate + availability-check. Returns ``(spec, None)`` or ``(None, error_dict)``.

    Raises ``ModuleNotFoundError`` when aihydro-modelling is not installed (the tool wrapper maps
    that to the structured ``MODELLING_NOT_INSTALLED`` envelope).
    """
    import aihydro_modelling as m
    from pydantic import ValidationError

    try:
        spec = m.ModelSpec.model_validate(raw)
    except ValidationError as ve:
        unknown = [e["loc"][0] for e in ve.errors() if e.get("type") == "extra_forbidden" and e.get("loc")]
        out = _err(
            "INVALID_SPEC", str(ve),
            hint="Call describe_model_space() to see valid fields and ranges.",
        )
        if unknown:
            out["unknown_keys"] = [str(k) for k in unknown]
            out["did_you_mean"] = m.unknown_key_hints(unknown)
        return None, out

    errors = m.validate(spec)
    if errors:
        return None, _err(
            "VALIDATION_ERROR",
            "; ".join(errors),
            validation_passed=False,
            errors=errors,
            hint=("Fix the errors listed above then retry. "
                  "Call describe_model_space() to see valid ranges."),
        )

    status = m.availability().get(spec.backend)
    if status is not None and not status.available:
        return None, _err(
            "BACKEND_UNAVAILABLE",
            f"Backend {spec.backend!r} cannot run on this host: {status.reason}",
            validation_passed=True,   # the spec is valid; the host just cannot run it
            backend=spec.backend,
            reason=status.reason,
            hint=("Pick an available backend (call describe_model_space() to see which) "
                  "or install the required dependency."),
        )
    return spec, None


def require_trainable(spec: Any, data: Any) -> None:
    """Raise ``ToolError`` when the resolved record cannot support ``spec`` (F7).

    Covers: no observed streamflow, a fit/select/report window the record does not cover
    after spin-up, and (neural) a sequence longer than the record.
    """
    import aihydro_modelling as m
    from aihydro_core.primitives.errors import ToolError

    errors = m.validate_with_data(spec, data)
    if errors:
        code = "INSUFFICIENT_DATA" if any("INSUFFICIENT_DATA" in e or "empty" in e for e in errors) \
            else "VALIDATION_ERROR"
        raise ToolError(
            code=code,
            message="; ".join(errors),
            recovery="Choose periods the observed record covers (fetch_streamflow_data shows its span).",
        )
