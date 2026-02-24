"""
Bounding box utilities for coordinate manipulation.
"""

from typing import Any


def normalize_bbox(
    bbox: dict[str, int] | list[int] | tuple[int, ...],
    image_width: int,
    image_height: int,
) -> dict[str, float]:
    """
    Normalize bounding box coordinates to [0.0-1.0].

    Args:
        bbox: Bounding box as dict with x0,y0,x1,y1 or list [x0,y0,x1,y1].
        image_width: Image width in pixels.
        image_height: Image height in pixels.

    Returns:
        Normalized bbox dict with x0,y0,x1,y1.
    """
    if isinstance(bbox, dict):
        x0, y0, x1, y1 = bbox["x0"], bbox["y0"], bbox["x1"], bbox["y1"]
    else:
        x0, y0, x1, y1 = bbox[0], bbox[1], bbox[2], bbox[3]

    return {
        "x0": x0 / image_width,
        "y0": y0 / image_height,
        "x1": x1 / image_width,
        "y1": y1 / image_height,
    }


def denormalize_bbox(
    bbox: dict[str, float],
    image_width: int,
    image_height: int,
) -> dict[str, int]:
    """
    Convert normalized bbox to pixel coordinates.

    Args:
        bbox: Normalized bbox dict.
        image_width: Image width in pixels.
        image_height: Image height in pixels.

    Returns:
        Bbox dict with pixel coordinates.
    """
    return {
        "x0": int(bbox["x0"] * image_width),
        "y0": int(bbox["y0"] * image_height),
        "x1": int(bbox["x1"] * image_width),
        "y1": int(bbox["y1"] * image_height),
    }


def compute_union_bbox(bboxes: list[dict[str, float]]) -> dict[str, float]:
    """
    Compute union bounding box of multiple boxes.

    Args:
        bboxes: List of bbox dicts with x0,y0,x1,y1.

    Returns:
        Union bbox dict.
    """
    if not bboxes:
        return {"x0": 0.0, "y0": 0.0, "x1": 0.0, "y1": 0.0}

    return {
        "x0": min(b["x0"] for b in bboxes),
        "y0": min(b["y0"] for b in bboxes),
        "x1": max(b["x1"] for b in bboxes),
        "y1": max(b["y1"] for b in bboxes),
    }


def bbox_overlap(
    bbox1: dict[str, float],
    bbox2: dict[str, float],
) -> float:
    """
    Calculate overlap ratio between two bboxes.

    Args:
        bbox1: First bbox.
        bbox2: Second bbox.

    Returns:
        Overlap ratio (intersection / union).
    """
    # Calculate intersection
    x0 = max(bbox1["x0"], bbox2["x0"])
    y0 = max(bbox1["y0"], bbox2["y0"])
    x1 = min(bbox1["x1"], bbox2["x1"])
    y1 = min(bbox1["y1"], bbox2["y1"])

    if x0 >= x1 or y0 >= y1:
        return 0.0

    intersection = (x1 - x0) * (y1 - y0)

    # Calculate union
    area1 = (bbox1["x1"] - bbox1["x0"]) * (bbox1["y1"] - bbox1["y0"])
    area2 = (bbox2["x1"] - bbox2["x0"]) * (bbox2["y1"] - bbox2["y0"])
    union = area1 + area2 - intersection

    if union == 0:
        return 0.0

    return intersection / union


def bbox_contains(
    outer: dict[str, float],
    inner: dict[str, float],
    tolerance: float = 0.0,
) -> bool:
    """
    Check if outer bbox contains inner bbox.

    Args:
        outer: Outer bounding box.
        inner: Inner bounding box.
        tolerance: Tolerance for boundary (normalized).

    Returns:
        True if inner is contained within outer.
    """
    return (
        inner["x0"] >= outer["x0"] - tolerance and
        inner["y0"] >= outer["y0"] - tolerance and
        inner["x1"] <= outer["x1"] + tolerance and
        inner["y1"] <= outer["y1"] + tolerance
    )


def bbox_iou(
    bbox1: dict[str, float],
    bbox2: dict[str, float],
) -> float:
    """
    Calculate Intersection over Union (IoU) for two bboxes.

    Args:
        bbox1: First bbox.
        bbox2: Second bbox.

    Returns:
        IoU score (0.0-1.0).
    """
    return bbox_overlap(bbox1, bbox2)


def expand_bbox(
    bbox: dict[str, float],
    padding: float,
    max_bounds: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0),
) -> dict[str, float]:
    """
    Expand a bounding box by padding.

    Args:
        bbox: Original bbox.
        padding: Amount to expand (normalized).
        max_bounds: Maximum bounds (x0, y0, x1, y1).

    Returns:
        Expanded bbox.
    """
    return {
        "x0": max(max_bounds[0], bbox["x0"] - padding),
        "y0": max(max_bounds[1], bbox["y0"] - padding),
        "x1": min(max_bounds[2], bbox["x1"] + padding),
        "y1": min(max_bounds[3], bbox["y1"] + padding),
    }


def bbox_center(bbox: dict[str, float]) -> tuple[float, float]:
    """
    Get center point of bbox.

    Args:
        bbox: Bounding box.

    Returns:
        Tuple of (center_x, center_y).
    """
    return (
        (bbox["x0"] + bbox["x1"]) / 2,
        (bbox["y0"] + bbox["y1"]) / 2,
    )


def bbox_area(bbox: dict[str, float]) -> float:
    """
    Calculate area of bbox.

    Args:
        bbox: Bounding box.

    Returns:
        Area (normalized).
    """
    return (bbox["x1"] - bbox["x0"]) * (bbox["y1"] - bbox["y0"])


def points_to_bbox(points: list[list[float]]) -> dict[str, float]:
    """
    Convert polygon points to axis-aligned bbox.

    Args:
        points: List of [x, y] points.

    Returns:
        Bbox dict.
    """
    x_coords = [p[0] for p in points]
    y_coords = [p[1] for p in points]

    return {
        "x0": min(x_coords),
        "y0": min(y_coords),
        "x1": max(x_coords),
        "y1": max(y_coords),
    }


def merge_nearby_bboxes(
    bboxes: list[dict[str, float]],
    distance_threshold: float = 0.01,
) -> list[dict[str, float]]:
    """
    Merge bboxes that are close together.

    Args:
        bboxes: List of bboxes.
        distance_threshold: Maximum distance to merge.

    Returns:
        List of merged bboxes.
    """
    if not bboxes:
        return []

    # Sort by x0 coordinate
    sorted_bboxes = sorted(bboxes, key=lambda b: (b["y0"], b["x0"]))

    merged = []
    current = sorted_bboxes[0].copy()

    for bbox in sorted_bboxes[1:]:
        # Check if close enough to merge
        if (bbox["x0"] <= current["x1"] + distance_threshold and
            bbox["y0"] <= current["y1"] + distance_threshold and
            bbox["y1"] >= current["y0"] - distance_threshold):
            # Merge
            current = compute_union_bbox([current, bbox])
        else:
            merged.append(current)
            current = bbox.copy()

    merged.append(current)
    return merged
