"""
Generate a CapCut PC project folder (draft_content.json + draft_meta_info.json).

The CapCut draft schema is not officially published. The structure implemented
here follows fields observed in CapCut PC 4.x-5.x drafts and community
reverse-engineering. If CapCut refuses to open the generated draft, open an
empty CapCut project, copy its draft_content.json here, and diff against this
generator to adjust fields.
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path

from .config import CANVAS_HEIGHT, CANVAS_WIDTH, FPS
from .plan import CaptionSegment, EditPlan, VideoSegment


def us(seconds: float) -> int:
    return int(round(seconds * 1_000_000))


def new_id() -> str:
    return str(uuid.uuid4()).upper()


def _video_material(clip_path: Path, duration_us: int) -> dict:
    return {
        "id": new_id(),
        "type": "video",
        "path": str(clip_path),
        "material_name": clip_path.name,
        "duration": duration_us,
        "width": CANVAS_WIDTH,
        "height": CANVAS_HEIGHT,
        "has_audio": False,
        "has_sound_separated": False,
        "crop": {"lower_left_x": 0, "lower_left_y": 1, "lower_right_x": 1, "lower_right_y": 1,
                 "upper_left_x": 0, "upper_left_y": 0, "upper_right_x": 1, "upper_right_y": 0},
        "crop_ratio": "free",
        "crop_scale": 1.0,
        "category_id": "",
        "category_name": "local",
        "extra_type_option": 0,
        "check_flag": 63487,
        "local_material_id": new_id(),
        "source_platform": 0,
        "stable": {"matrix_path": "", "stable_level": 0, "time_range": {"duration": 0, "start": 0}},
        "team_id": "",
        "type_specific_id": "",
    }


def _audio_material(path: Path, duration_us: int) -> dict:
    return {
        "id": new_id(),
        "type": "extract_music",
        "path": str(path),
        "name": path.name,
        "duration": duration_us,
        "category_id": "local",
        "category_name": "local_music",
        "effect_id": "",
        "source_platform": 0,
        "team_id": "",
    }


def _text_material(text: str, style: dict) -> dict:
    captions = style.get("captions", {})
    primary = captions.get("primary_color", "#FFFFFF").lstrip("#")
    stroke = captions.get("stroke_color", "#000000").lstrip("#")

    def hex_to_rgb01(h: str) -> list[float]:
        return [int(h[i:i+2], 16) / 255.0 for i in (0, 2, 4)]

    text_color = hex_to_rgb01(primary)
    stroke_color = hex_to_rgb01(stroke)

    content = {
        "text": text,
        "styles": [{
            "fill": {"content": {"solid": {"color": text_color}}, "alpha": 1.0},
            "strokes": [{
                "content": {"solid": {"color": stroke_color}},
                "width": 0.08,
            }],
            "font": {
                "path": "",
                "id": "",
                "title": captions.get("font_style", ""),
            },
            "size": 15.0,
            "bold": False,
            "italic": False,
            "underline": False,
            "range": [0, len(text)],
        }],
    }
    return {
        "id": new_id(),
        "type": "subtitle",
        "content": json.dumps(content, ensure_ascii=False),
        "text": text,
        "font_name": captions.get("font_style", ""),
        "font_path": "",
        "font_size": 15.0,
        "text_color": "#" + primary,
        "border_color": "#" + stroke,
        "border_width": 0.08,
        "has_shadow": False,
        "alignment": 1,
        "text_alpha": 1.0,
        "background_color": "",
        "background_alpha": 0.0,
    }


def _canvas_material() -> dict:
    return {"id": new_id(), "type": "canvas_color", "color": "#000000", "image": "", "album_image": ""}


def _video_segment(material_id: str, canvas_id: str, seg: VideoSegment) -> dict:
    return {
        "id": new_id(),
        "material_id": material_id,
        "source_timerange": {"start": us(seg.source_start), "duration": us(seg.source_end - seg.source_start)},
        "target_timerange": {"start": us(seg.timeline_start), "duration": us(seg.timeline_end - seg.timeline_start)},
        "extra_material_refs": [canvas_id],
        "speed": 1.0,
        "volume": 1.0,
        "visible": True,
        "clip": {
            "alpha": 1.0,
            "flip": {"horizontal": False, "vertical": False},
            "rotation": 0.0,
            "scale": {"x": 1.0, "y": 1.0},
            "transform": {"x": 0.0, "y": 0.0},
        },
        "enable_adjust": True,
        "enable_color_curves": True,
        "enable_color_wheels": True,
        "enable_lut": True,
        "enable_smart_color_adjust": False,
        "last_nonzero_volume": 1.0,
        "reverse": False,
        "track_attribute": 0,
        "track_render_index": 0,
        "uniform_scale": {"on": True, "value": 1.0},
    }


def _audio_segment(material_id: str, duration_us: int) -> dict:
    return {
        "id": new_id(),
        "material_id": material_id,
        "source_timerange": {"start": 0, "duration": duration_us},
        "target_timerange": {"start": 0, "duration": duration_us},
        "speed": 1.0,
        "volume": 1.0,
        "visible": True,
        "extra_material_refs": [],
    }


def _text_segment(material_id: str, cap: CaptionSegment) -> dict:
    return {
        "id": new_id(),
        "material_id": material_id,
        "source_timerange": None,
        "target_timerange": {"start": us(cap.start), "duration": us(cap.end - cap.start)},
        "speed": 1.0,
        "volume": 1.0,
        "visible": True,
        "extra_material_refs": [],
        "clip": {
            "alpha": 1.0,
            "flip": {"horizontal": False, "vertical": False},
            "rotation": 0.0,
            "scale": {"x": 1.0, "y": 1.0},
            "transform": {"x": 0.0, "y": 0.0},
        },
        "render_index": 14000,
        "track_render_index": 1,
    }


def _track(track_type: str, segments: list[dict]) -> dict:
    return {
        "id": new_id(),
        "type": track_type,
        "attribute": 0,
        "flag": 0,
        "segments": segments,
    }


def build_draft_content(plan: EditPlan) -> dict:
    total_us = us(plan.narration_duration)

    videos: list[dict] = []
    canvases: list[dict] = []
    video_segments: list[dict] = []

    for seg in plan.video_segments:
        mat = _video_material(seg.clip.path, us(seg.clip.duration))
        canvas = _canvas_material()
        videos.append(mat)
        canvases.append(canvas)
        video_segments.append(_video_segment(mat["id"], canvas["id"], seg))

    audio_mat = _audio_material(plan.narration_path, total_us)
    audio_seg = _audio_segment(audio_mat["id"], total_us)

    text_materials: list[dict] = []
    text_segments: list[dict] = []
    for cap in plan.captions:
        tm = _text_material(cap.text, plan.style)
        text_materials.append(tm)
        text_segments.append(_text_segment(tm["id"], cap))

    return {
        "id": new_id(),
        "version": 360000,
        "new_version": "93.0.0",
        "fps": FPS,
        "duration": total_us,
        "canvas_config": {"width": CANVAS_WIDTH, "height": CANVAS_HEIGHT, "ratio": "9:16"},
        "color_space": 0,
        "materials": {
            "videos": videos,
            "audios": [audio_mat],
            "texts": text_materials,
            "canvases": canvases,
            "effects": [],
            "stickers": [],
            "transitions": [],
            "audio_effects": [],
            "audio_fades": [],
            "beats": [],
            "speeds": [],
            "masks": [],
            "placeholder_infos": [],
            "sound_channel_mappings": [],
            "video_effects": [],
            "loudnesses": [],
        },
        "tracks": [
            _track("video", video_segments),
            _track("audio", [audio_seg]),
            _track("text", text_segments),
        ],
        "platform": {"app_id": 359, "app_source": "cc", "app_version": "5.1.0", "device_id": "",
                     "hard_disk_id": "", "mac_address": "", "os": "windows", "os_version": "10"},
        "last_modified_platform": {"app_id": 359, "app_source": "cc", "app_version": "5.1.0",
                                    "device_id": "", "hard_disk_id": "", "mac_address": "",
                                    "os": "windows", "os_version": "10"},
    }


def build_meta_info(project_name: str, plan: EditPlan) -> dict:
    now_us = int(time.time() * 1_000_000)
    return {
        "cloud_package_completed_time": "",
        "draft_cloud_capcut_purchase_info": "",
        "draft_cloud_last_action_download": False,
        "draft_cloud_materials": [],
        "draft_cloud_purchase_info": "",
        "draft_cloud_template_id": "",
        "draft_cloud_tutorial_info": "",
        "draft_cloud_videocut_purchase_info": "",
        "draft_cover": "draft_cover.jpg",
        "draft_deeplink_url": "",
        "draft_enterprise_info": {"draft_enterprise_extra": "",
                                   "draft_enterprise_id": "",
                                   "draft_enterprise_name": "",
                                   "enterprise_material": []},
        "draft_fold_path": "",
        "draft_id": new_id(),
        "draft_is_ai_packaging_used": False,
        "draft_is_ai_shorts": False,
        "draft_is_ai_translate": False,
        "draft_is_article_video_draft": False,
        "draft_is_from_deeplink": "false",
        "draft_is_invisible": False,
        "draft_materials": [
            {"type": 0, "value": [{"creation_time": now_us, "display_name": s.clip.path.name,
                                    "filter_type": 0, "id": new_id(), "import_time": now_us,
                                    "import_time_ms": now_us // 1000,
                                    "item_source": 1, "md5": "", "metetype": "video",
                                    "roughcut_time_range": {"duration": us(s.clip.duration), "start": 0},
                                    "sub_time_range": {"duration": -1, "start": -1},
                                    "type": 0, "file_Path": str(s.clip.path)}
                                   for s in plan.video_segments]},
        ],
        "draft_name": project_name,
        "draft_new_version": "",
        "draft_removable_storage_device": "",
        "draft_root_path": "",
        "draft_segment_extra_info": [],
        "draft_timeline_materials_size_": 0,
        "draft_type": "",
        "tm_draft_cloud_completed": "",
        "tm_draft_cloud_modified": 0,
        "tm_draft_create": now_us,
        "tm_draft_modified": now_us,
        "tm_draft_removed": 0,
        "tm_duration": us(plan.narration_duration),
    }


def write_draft(project_name: str, plan: EditPlan, draft_root: Path) -> Path:
    project_dir = draft_root / project_name
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "draft_content.json").write_text(
        json.dumps(build_draft_content(plan), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (project_dir / "draft_meta_info.json").write_text(
        json.dumps(build_meta_info(project_name, plan), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    resources = project_dir / "Resources"
    resources.mkdir(exist_ok=True)
    return project_dir


def copy_assets(plan: EditPlan, project_dir: Path) -> EditPlan:
    """Copy source files into the project Resources folder so CapCut can find them."""
    res = project_dir / "Resources"
    res.mkdir(exist_ok=True)
    new_narration = res / plan.narration_path.name
    if not new_narration.exists():
        shutil.copy2(plan.narration_path, new_narration)
    plan.narration_path = new_narration
    for seg in plan.video_segments:
        dest = res / seg.clip.path.name
        if not dest.exists():
            shutil.copy2(seg.clip.path, dest)
        seg.clip.path = dest
    return plan
