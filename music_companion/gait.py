"""可解释的行走步长与节拍估算。

这里的 ``step_length_cm`` 指单步（一次落脚到下一次落脚）的粗略长度，
不是左右脚合计的双步 gait stride。它用于教学演示与音乐节拍联动，不是
医学、运动处方或精密步态测量。
"""

from __future__ import annotations

from typing import Any


MIN_CADENCE = 80.0
MAX_CADENCE = 140.0
MIN_SPEED_KMH = 1.0
MAX_SPEED_KMH = 7.0


def _number(value: Any, name: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}必须是数字") from exc
    if number <= 0:
        raise ValueError(f"{name}必须大于 0")
    return number


def build_walking_guidance(
    *,
    height_cm: float | None = None,
    leg_length_cm: float | None = None,
    measured_step_length_cm: float | None = None,
    step_length_cm: float | None = None,
    target_speed_kmh: float | None = None,
    fallback_cadence: float = 112.0,
) -> dict[str, Any]:
    """根据手动测量值或透明启发式给出行走音乐节拍提示。

    ``measured_step_length_cm`` 优先于 ``step_length_cm``（后者是便于 API
    调用者使用的同义输入），其次使用腿长的 0.7 倍，再使用身高的 0.415
    倍。没有身体数据时仅使用 60 cm 的课堂演示默认值。
    """
    height = _number(height_cm, "身高")
    leg_length = _number(leg_length_cm, "腿长")
    measured = _number(measured_step_length_cm, "实测单步长度")
    alias_step = _number(step_length_cm, "单步长度")
    target_speed = _number(target_speed_kmh, "目标速度")
    cadence_default = _number(fallback_cadence, "默认步频") or 112.0

    if height is not None and not 80 <= height <= 250:
        raise ValueError("身高需在 80 到 250 之间")
    if leg_length is not None and not 30 <= leg_length <= 160:
        raise ValueError("腿长需在 30 到 160 之间")
    if measured is not None and not 20 <= measured <= 200:
        raise ValueError("实测单步长度需在 20 到 200 之间")
    if alias_step is not None and not 20 <= alias_step <= 200:
        raise ValueError("单步长度需在 20 到 200 之间")
    if target_speed is not None and not MIN_SPEED_KMH <= target_speed <= MAX_SPEED_KMH:
        raise ValueError(f"目标速度需在 {MIN_SPEED_KMH:g} 到 {MAX_SPEED_KMH:g} km/h 之间")
    if not MIN_CADENCE <= cadence_default <= MAX_CADENCE:
        cadence_default = max(MIN_CADENCE, min(MAX_CADENCE, cadence_default))

    assumptions: list[str] = [
        "step_length_cm 表示每一步的单步长度，不是双步 gait stride。",
        "这是课堂教学用的粗略估算，不代表医学或运动处方精度。",
    ]
    warnings: list[str] = [
        "身体比例与实际步态存在个体差异；建议用平地实测单步长度复核。",
    ]
    if measured is not None:
        step_length = measured
        method = "measured_step_length_cm"
        assumptions.append("优先采用手动填写的实测单步长度。")
    elif alias_step is not None:
        step_length = alias_step
        method = "step_length_cm"
        assumptions.append("采用手动填写的单步长度。")
    elif leg_length is not None:
        step_length = leg_length * 0.7
        method = "leg_length_heuristic"
        assumptions.append("未提供实测值，按单步长度 ≈ 0.7 × 腿长估算。")
    elif height is not None:
        step_length = height * 0.415
        method = "height_heuristic"
        assumptions.append("未提供实测值或腿长，按单步长度 ≈ 0.415 × 身高估算。")
    else:
        step_length = 60.0
        method = "default_assumption"
        assumptions.append("未提供身体数据，课堂演示暂按 60 cm 单步长度估算。")

    step_m = step_length / 100.0
    if target_speed is None:
        cadence_raw = cadence_default
        cadence = round(cadence_raw, 6)
        estimated_speed = round(cadence * step_m * 60.0 / 1000.0, 3)
        achievable: bool | None = None
        warnings.append("未指定目标速度，步频沿用现有行走启发式；estimated_speed 仅供参考。")
    else:
        cadence_raw = target_speed * 1000.0 / (60.0 * step_m)
        cadence = round(max(MIN_CADENCE, min(MAX_CADENCE, cadence_raw)), 6)
        estimated_speed = round(cadence * step_m * 60.0 / 1000.0, 3)
        achievable = MIN_CADENCE <= cadence_raw <= MAX_CADENCE
        if not achievable:
            warnings.append(
                f"按该步长达到 {target_speed:g} km/h 需要 {cadence_raw:.1f} steps/min，"
                "超出 80–140 steps/min 教学节拍范围，已限制到可演示范围。"
            )

    limitations = [
        "音乐按 walking 场景一拍对应一步；真实步频应以个人实测为准。",
        "节拍范围限制为 80–140 steps/min，超范围时 estimated_speed 是限制后的估计，不能视为达成目标。",
        "结果仅用于教学和音乐联动，不用于医学判断、康复或运动处方。",
    ]
    return {
        "method": method,
        "assumptions": assumptions,
        "step_length_cm": round(step_length, 2),
        "cadence": cadence,
        "cadence_unit": "steps/min",
        "target_speed_kmh": round(target_speed, 3) if target_speed is not None else None,
        "estimated_speed": estimated_speed,
        "estimated_speed_unit": "km/h",
        "achievable": achievable,
        "warnings": warnings,
        "limitations": limitations,
    }
