"""Shared validation at both Python and MCP tool entry points."""

import functools
import asyncio
from contextlib import AsyncExitStack
import inspect
import math
import os
import re
from pathlib import Path
import struct
import weakref
from PIL import Image

MAX_DIMENSION = 4096
MAX_PIXELS = 4_194_304
MAX_ITEMS = 65_536
MAX_COORDINATE = 8192
MAX_FRAMES = 1024
MAX_TEXT = 4096
MAX_SCRIPT_BYTES = 1_048_576
MAX_FILE_BYTES = 134_217_728
MAX_WORK = 16_777_216
_file_locks = weakref.WeakValueDictionary()


class _FileLock:
    """Allow a tool to call another tool on the same file in the same task."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.owner = None
        self.depth = 0

    async def __aenter__(self):
        task = asyncio.current_task()
        if self.owner is not task:
            await self.lock.acquire()
            self.owner = task
        self.depth += 1
        return self

    async def __aexit__(self, *exc):
        self.depth -= 1
        if not self.depth:
            self.owner = None
            self.lock.release()

PATH_ARGUMENTS = {
    "filename", "source_filename", "target_filename", "output_filename",
    "data_filename", "image_path", "directory", "output_directory",
}
COORDINATES = {
    "x", "y", "x1", "y1", "x2", "y2", "center_x", "center_y", "dest_x", "dest_y",
    "start_x", "start_y", "end_x", "end_y", "dx", "dy", "shadow_dx", "shadow_dy",
    "amplitude_x", "amplitude_y", "col", "row", "letter_spacing",
}


def validate_path(value: str) -> str:
    """Reject parent syntax before normalization; optionally scope real paths."""
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("path must be a nonempty string without NUL bytes")
    if ".." in value.replace("\\", "/").split("/"):
        raise ValueError("parent directory traversal not allowed")
    path = Path(value).expanduser().resolve()
    root = os.getenv("ASEPRITE_WORKSPACE_ROOT")
    if root and not path.is_relative_to(Path(root).expanduser().resolve()):
        raise ValueError("path is outside ASEPRITE_WORKSPACE_ROOT")
    return str(path)


def check_area(width: int, height: int, *, limit: int = MAX_PIXELS) -> None:
    if width < 1 or height < 1 or width > MAX_DIMENSION or height > MAX_DIMENSION or width * height > limit:
        raise ValueError(f"dimensions must be 1..{MAX_DIMENSION}, with at most {limit} pixels")


def sprite_size(path: str) -> tuple[int, int, int] | None:
    """Inspect the Aseprite header without decompressing an untrusted file."""
    if not os.path.isfile(path):
        return None
    if os.path.getsize(path) > MAX_FILE_BYTES:
        raise ValueError(f"input file exceeds {MAX_FILE_BYTES} bytes")
    with open(path, "rb") as file:
        header = file.read(14)
    if len(header) < 14 or struct.unpack_from("<H", header, 4)[0] != 0xA5E0:
        if Path(path).suffix.lower() in {".png", ".gif", ".jpg", ".jpeg", ".webp", ".bmp", ".ico"}:
            with Image.open(path) as image:
                check_area(*image.size)
                frames = getattr(image, "n_frames", 1)
                if frames > MAX_FRAMES or image.width * image.height * frames > MAX_WORK:
                    raise ValueError("image exceeds frame or total pixel limits")
                return *image.size, frames
        return None
    frames, width, height = struct.unpack_from("<HHH", header, 6)
    check_area(width, height)
    if not 1 <= frames <= MAX_FRAMES or width * height * frames > MAX_WORK:
        raise ValueError("sprite exceeds frame or total pixel limits")
    return width, height, frames


def validate_arguments(name: str, values: dict) -> dict:
    """Bound user-controlled loops, allocations, paths and generated Lua."""
    values = values.copy()
    # Validate and lock the actual output path, including automatic extensions.
    extension = {"export_frame": "png", "export_spritesheet": "png", "copy_sprite": "aseprite"}.get(name)
    if name == "export_sprite":
        if not re.fullmatch(r"[a-zA-Z0-9]{1,16}", values["format"]):
            raise ValueError("export format must be a simple file extension")
        extension = values["format"].lower()
    if extension and not values["output_filename"].lower().endswith(f".{extension}"):
        values["output_filename"] += f".{extension}"
    for key, value in values.items():
        if value is None:
            continue
        if key in PATH_ARGUMENTS and value:
            values[key] = validate_path(value)
            if key in {"output_filename", "data_filename"} and any(char in value for char in "{}"):
                raise ValueError("output path templates are not supported")
        if isinstance(value, str):
            limit = MAX_SCRIPT_BYTES if key == "script" else (MAX_TEXT if key == "text" else 4096)
            if len(value.encode("utf-8")) > limit:
                raise ValueError(f"{key} exceeds {limit} bytes")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if abs(value) > 1_000_000 or (isinstance(value, float) and not math.isfinite(value)):
                raise ValueError(f"{key} must be finite and within supported bounds")
            if key in COORDINATES and abs(value) > MAX_COORDINATE:
                raise ValueError(f"{key} must be within +/-{MAX_COORDINATE}")
            if ("frame" in key or key in {"count", "times", "before", "after", "steps", "tile_index"}) and abs(value) > MAX_FRAMES:
                raise ValueError(f"{key} exceeds {MAX_FRAMES}")
            if key in {"width", "height", "tile_width", "tile_height", "radius", "radius_x", "radius_y", "padding"} and abs(value) > MAX_DIMENSION:
                raise ValueError(f"{key} exceeds {MAX_DIMENSION}")
            if key == "thickness" and not 1 <= value <= 64:
                raise ValueError("thickness must be 1..64")
            if key == "size" and value > 256:
                raise ValueError("size must be 1..256")
            if key in {"bold", "outline_width"} and not 0 <= value <= 16:
                raise ValueError(f"{key} must be 0..16")
            if key in {"start_scale", "end_scale", "scale"} and not 0 < value <= 64:
                raise ValueError(f"{key} must be > 0 and <= 64")
        if isinstance(value, list):
            limit = MAX_ITEMS if key in {"pixels", "tiles"} else MAX_FRAMES
            if len(value) > limit:
                raise ValueError(f"{key} exceeds {limit} entries")
            for item in value:
                if isinstance(item, dict):
                    for field, number in item.items():
                        if field in COORDINATES or field == "tile_index":
                            if type(number) is not int or abs(number) > MAX_COORDINATE:
                                raise ValueError(f"{key}.{field} must be an integer within +/-{MAX_COORDINATE}")
                        elif not isinstance(number, str) or len(number) > 64:
                            raise ValueError(f"invalid {key}.{field}")
                elif not isinstance(item, str) or len(item) > 4096:
                    raise ValueError(f"invalid {key} entry")
    if "font" in values and any(c in values["font"] for c in ("/", "\\")):
        values["font"] = validate_path(values["font"])
    if name == "run_lua_script" and os.getenv("ASEPRITE_WORKSPACE_ROOT"):
        raise ValueError("raw Lua is disabled when ASEPRITE_WORKSPACE_ROOT is configured")
    if "width" in values and "height" in values and values["width"] > 0 and values["height"] > 0:
        check_area(values["width"], values["height"])
    if name == "create_tilemap_layer":
        check_area(values["tile_width"], values["tile_height"])
    if values.get("image_path"):
        sprite_size(values["image_path"])
    for key in ("filename", "source_filename", "target_filename"):
        if not values.get(key):
            continue
        size = sprite_size(values[key])
        if not size:
            continue
        w, h, frames = size
        scale = max(values.get("scale", 1), values.get("start_scale", 1), values.get("end_scale", 1))
        check_area(max(1, math.ceil(w * scale)), max(1, math.ceil(h * scale)))
        if name == "add_frames" and (frames + values["count"] > MAX_FRAMES or w * h * (frames + values["count"]) > MAX_WORK):
            raise ValueError("resulting sprite exceeds frame or total pixel limits")
        if name == "add_frame" and (frames + 1 > MAX_FRAMES or w * h * (frames + 1) > MAX_WORK):
            raise ValueError("resulting sprite exceeds frame or total pixel limits")
        if name == "duplicate_frame_range":
            added = (values["end_frame"] - values["start_frame"] + 1) * values["times"]
            if frames + added > MAX_FRAMES or w * h * (frames + added) > MAX_WORK:
                raise ValueError("resulting sprite exceeds frame or total pixel limits")
        if name == "export_spritesheet":
            padding = max(0, values["padding"])
            if (w * scale + padding) * (h * scale + padding) * frames > MAX_PIXELS:
                raise ValueError("sprite sheet exceeds pixel limit")
    if name in {"draw_line", "draw_line_at", "draw_path", "draw_polygon"}:
        points = values.get("points") or [{"x": values.get("x1", 0), "y": values.get("y1", 0)}, {"x": values.get("x2", 0), "y": values.get("y2", 0)}]
        length = sum(max(abs(a["x"] - b["x"]), abs(a["y"] - b["y"])) + 1 for a, b in zip(points, points[1:] + points[:1]))
        if length * (values.get("thickness", 1) + 1) ** 2 > MAX_WORK:
            raise ValueError("drawing exceeds work limit")
    if name in {"get_pixels_rect", "get_composite_rect"} and values["width"] * values["height"] > MAX_ITEMS:
        raise ValueError(f"pixel reads are limited to {MAX_ITEMS} pixels per call")
    return values


def guard_tool(function):
    signature = inspect.signature(function)

    @functools.wraps(function)
    async def guarded(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        try:
            values = validate_arguments(function.__name__, bound.arguments)
        except (ValueError, OSError) as error:
            return f"Invalid input: {error}"
        # Serialize whole load/modify/save operations on a shared file. The
        # subprocess semaphore still permits independent files to run together.
        paths = sorted({value for key, value in values.items() if key in PATH_ARGUMENTS and value})
        loop_id = id(asyncio.get_running_loop())
        locks = []
        for path in paths:
            key = (loop_id, path)
            lock = _file_locks.get(key)
            if lock is None:
                lock = _FileLock()
                _file_locks[key] = lock
            locks.append(lock)
        async with AsyncExitStack() as stack:
            for lock in locks:
                await stack.enter_async_context(lock)
            return await function(**values)

    return guarded
