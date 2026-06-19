# config_mockup.py — Load và cấp phát config ghép art
# =====================================================
# Đọc configs.json cùng thư mục để lấy tọa độ anchor, scale,
# blend mode cho từng pose (single) và mockup 2 mặt (sides).
#
# ─── Giải thích các key ───
# anchor        : [x%, y%] — tâm art trên mockup
# scale         : tỉ lệ kích thước art so với mockup
# scale_by      : 'height' | 'width' — chiều chuẩn để scale
# rotation      : độ xoay chiều kim đồng hồ (degree)
# blend_mode    : 'multiply' | 'overlay' | 'screen' | 'normal'
# blend_opacity : 0.0–1.0
# shadow_opacity: 0.0–1.0
# use_warp      : true/false
# warp_pts      : [[x%,y%] × 4] TL, TR, BR, BL — null nếu use_warp = false
# shape_overrides: ghi đè config theo shape art: "square"|"portrait"|"landscape"

from __future__ import annotations
import json
from pathlib import Path


# ── Config file path ──────────────────────────────────────────────────────────
_CONFIG_FILE = Path(__file__).parent / "configs.json"


# ── Load JSON ─────────────────────────────────────────────────────────────────
def _load_configs() -> dict:
    if not _CONFIG_FILE.exists():
        raise FileNotFoundError(
            f"[config_mockup] Không tìm thấy configs.json tại: {_CONFIG_FILE}\n"
            f"  → Copy file configs.json vào thư mục mockup-generator/"
        )
    with open(_CONFIG_FILE, encoding="utf-8") as f:
        data = json.load(f)

    def _fix_anchors(cfg: dict) -> dict:
        if "anchor" in cfg and isinstance(cfg["anchor"], list):
            cfg["anchor"] = tuple(cfg["anchor"])
        for shape_cfg in cfg.get("shape_overrides", {}).values():
            if "anchor" in shape_cfg and isinstance(shape_cfg["anchor"], list):
                shape_cfg["anchor"] = tuple(shape_cfg["anchor"])
        return cfg

    for cfg in data.get("single", {}).values():
        _fix_anchors(cfg)

    for sides_cfg in data.get("sides", {}).values():
        for sub in ("config_front", "config_back"):
            if sub in sides_cfg:
                _fix_anchors(sides_cfg[sub])

    return data


_DATA = _load_configs()

SINGLE_CONFIGS   = _DATA.get("single",   {})
SIDES_CONFIGS    = _DATA.get("sides",    {})
MATCHING_CONFIGS = _DATA.get("matching", {})
CATALOG_CONFIGS  = _DATA.get("catalog",  {})


# ── Reload ────────────────────────────────────────────────────────────────────
def reload_configs():
    """Reload configs.json từ disk — dùng khi chỉnh file và muốn áp ngay."""
    global _DATA
    _DATA = _load_configs()
    SINGLE_CONFIGS.clear();   SINGLE_CONFIGS.update(_DATA.get("single",   {}))
    SIDES_CONFIGS.clear();    SIDES_CONFIGS.update(_DATA.get("sides",    {}))
    MATCHING_CONFIGS.clear(); MATCHING_CONFIGS.update(_DATA.get("matching", {}))
    CATALOG_CONFIGS.clear();  CATALOG_CONFIGS.update(_DATA.get("catalog",  {}))
    print(f"[config_mockup] Đã reload từ {_CONFIG_FILE}")


# ── Accessors ─────────────────────────────────────────────────────────────────
def get_single_config(key: str) -> dict:
    if key not in SINGLE_CONFIGS:
        raise ValueError(
            f"[config_mockup] Single config '{key}' không tồn tại. "
            f"Có: {list(SINGLE_CONFIGS)}"
        )
    return SINGLE_CONFIGS[key]


def get_sides_config(key: str) -> dict:
    if key not in SIDES_CONFIGS:
        raise ValueError(
            f"[config_mockup] Sides config '{key}' không tồn tại. "
            f"Có: {list(SIDES_CONFIGS)}"
        )
    return SIDES_CONFIGS[key]


def get_matching_config(key: str) -> dict:
    if key not in MATCHING_CONFIGS:
        raise ValueError(
            f"[config_mockup] Matching config '{key}' không tồn tại. "
            f"Có: {list(MATCHING_CONFIGS)}"
        )
    return MATCHING_CONFIGS[key]


def get_catalog_config(key: str) -> dict:
    if key not in CATALOG_CONFIGS:
        raise ValueError(
            f"[config_mockup] Catalog config '{key}' không tồn tại. "
            f"Có: {list(CATALOG_CONFIGS)}"
        )
    return CATALOG_CONFIGS[key]


def list_configs():
    """In danh sách tất cả configs đang có."""
    print(f"\n[Đọc từ: {_CONFIG_FILE}]")

    print(f"\n[Single configs]  ({len(SINGLE_CONFIGS)} configs)")
    for k, v in SINGLE_CONFIGS.items():
        print(f"  {k}: anchor={v.get('anchor')}  scale={v.get('scale')}  "
              f"blend={v.get('blend_mode')}  rotation={v.get('rotation', 0)}°")

    print(f"\n[Sides configs]  ({len(SIDES_CONFIGS)} configs)")
    for k, v in SIDES_CONFIGS.items():
        fa = v["config_front"].get("anchor")
        ba = v["config_back"].get("anchor")
        print(f"  {k}: front anchor={fa}  back anchor={ba}")

    print(f"\n[Matching configs]  ({len(MATCHING_CONFIGS)} configs)")
    for k in MATCHING_CONFIGS:
        print(f"  {k}")

    print(f"\n[Catalog configs]  ({len(CATALOG_CONFIGS)} configs)")
    for k, v in CATALOG_CONFIGS.items():
        print(f"  {k}: {len(v.get('positions', []))} vị trí")


# ── Shape-aware config resolver ───────────────────────────────────────────────
def resolve_config_for_shape(config: dict, shape: str) -> dict:
    """
    Merge shape_overrides vào config gốc theo shape của art.

    shape: 'square' | 'portrait' | 'landscape'
    Nếu không có override cho shape → trả về config gốc không đổi.
    """
    shape_cfg = config.get("shape_overrides", {}).get(shape, {})
    if not shape_cfg:
        return config
    resolved = {k: v for k, v in config.items() if k != "shape_overrides"}
    resolved.update(shape_cfg)
    return resolved
