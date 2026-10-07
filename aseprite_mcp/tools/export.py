import glob
import os
import shutil
import tempfile
from ..core.commands import AsepriteCommand, lua_escape, reject_traversal
from ..core.lua import FIND_LAYER, NORMALIZE_CEL
from .. import mcp
from ..core.security import validate_path


async def _save_export(args: list[str], target: str, *, single: bool = False):
    """Stage native output, then validate every frame-numbered destination."""
    with tempfile.TemporaryDirectory(prefix="aseprite-mcp-export-") as scratch:
        output_path = os.path.join(scratch, os.path.basename(target))
        success, output = await AsepriteCommand.run_command_async(args + ["--save-as", output_path])
        if not success:
            return False, output
        files = sorted(entry.path for entry in os.scandir(scratch) if entry.is_file(follow_symlinks=False))
        if not files or (single and len(files) != 1):
            return False, "Aseprite exited 0 but did not produce the requested export"
        try:
            destinations = [validate_path(target if single else os.path.join(os.path.dirname(target), os.path.basename(path))) for path in files]
        except (ValueError, OSError) as error:
            return False, str(error)
        for source, destination in zip(files, destinations):
            shutil.move(source, destination)
        return True, output

@mcp.tool()
async def export_sprite(filename: str, output_filename: str, format: str = "png") -> str:
    """Export the Aseprite file to another format.

    Args:
        filename: Name of the Aseprite file to export
        output_filename: Name of the output file
        format: Output format (default: "png", can be "png", "gif", "jpg", etc.)
    """
    if not os.path.exists(filename):
        return f"File {filename} not found"
    
    # Make sure format is lowercase
    format = format.lower()
    
    # Ensure output filename has the correct extension
    if not output_filename.lower().endswith(f".{format}"):
        output_filename = f"{output_filename}.{format}"
    
    success, output = await _save_export(["--batch", filename], output_filename)

    if success:
        return f"Sprite exported successfully to {output_filename}"
    else:
        return f"Failed to export sprite: {output}"

@mcp.tool()
async def copy_sprite(filename: str, output_filename: str, overwrite: bool = False) -> str:
    """Copy a sprite to a new Aseprite file.

    Args:
        filename: Name of the Aseprite file to copy
        output_filename: Name of the output .aseprite file
        overwrite: Whether to overwrite if output exists
    """
    if not os.path.exists(filename):
        return f"File {filename} not found"

    if not output_filename.lower().endswith(".aseprite"):
        output_filename = f"{output_filename}.aseprite"

    err = reject_traversal(output_filename)
    if err:
        return err

    if os.path.exists(output_filename) and not overwrite:
        return f"Output file {output_filename} already exists"

    safe_path = lua_escape(output_filename.replace("\\", "/"))
    script = f"""
    local spr = app.activeSprite
    if not spr then print("ERROR:No active sprite") return end

    spr:saveAs("{safe_path}")
    print("OK")
    """

    success, output = await AsepriteCommand.execute_lua_script_checked_async(script, filename)
    if success and not os.path.exists(output_filename):
        success = False
        output = "Aseprite exited 0 but wrote no file"
    if success:
        return f"Sprite copied to {output_filename}"
    return f"Failed to copy sprite: {output}"


@mcp.tool()
async def export_frame(
    filename: str,
    frame_index: int,
    output_filename: str,
    scale: int = 1,
) -> str:
    """Export a single frame as a PNG, optionally scaled up.

    Use this for visual feedback while drawing: export at scale 8-10 and
    open the PNG to inspect the result, then keep iterating.

    Args:
        filename: Aseprite file to export
        frame_index: Frame index starting at 1
        output_filename: Output PNG path
        scale: Integer nearest-neighbor scale factor (default 1)
    """
    if not os.path.exists(filename):
        return f"File {filename} not found"
    if scale < 1 or scale > 64:
        return "scale must be between 1 and 64"
    err = reject_traversal(output_filename)
    if err:
        return err
    if not output_filename.lower().endswith(".png"):
        output_filename = f"{output_filename}.png"

    f0 = frame_index - 1  # CLI --frame-range is 0-based
    args = [
        "--batch", filename,
        "--frame-range", f"{f0},{f0}",
        "--scale", str(scale),
    ]
    success, output = await _save_export(args, output_filename, single=True)
    if not success:
        return f"Failed to export frame: {output}"

    return f"Frame {frame_index} exported to {output_filename} at {scale}x"


@mcp.tool()
async def export_spritesheet(
    filename: str,
    output_filename: str,
    sheet_type: str = "horizontal",
    data_filename: str = "",
    scale: int = 1,
    padding: int = 0,
    tag_name: str = "",
    data_format: str = "json-array",
    list_tags: bool = False,
) -> str:
    """Export frames as a sprite sheet, optionally with a JSON data file.

    Args:
        filename: Aseprite file to export
        output_filename: Output sheet image path (PNG)
        sheet_type: Layout: "horizontal", "vertical", "rows", "columns", or "packed"
        data_filename: Optional path for a JSON metadata file
        scale: Integer scale factor applied before packing (default 1)
        padding: Padding in pixels between frames (default 0)
        tag_name: Only include frames of this animation tag (default: all frames)
        data_format: JSON format for the data file: "json-array" (default) or "json-hash"
        list_tags: Include animation tag metadata in the JSON data file (default: False)
    """
    if not os.path.exists(filename):
        return f"File {filename} not found"
    if sheet_type not in ("horizontal", "vertical", "rows", "columns", "packed"):
        return "sheet_type must be one of: horizontal, vertical, rows, columns, packed"
    if scale < 1 or scale > 64:
        return "scale must be between 1 and 64"
    if padding < 0:
        return "padding must be >= 0"
    if data_format not in ("json-array", "json-hash"):
        return "data_format must be 'json-array' or 'json-hash'"
    err = reject_traversal(output_filename)
    if err:
        return err
    if not output_filename.lower().endswith(".png"):
        output_filename = f"{output_filename}.png"

    args = ["--batch"]
    if tag_name:
        # Frame filters only apply to --sheet when they appear before
        # the input file; resolve the tag to a 0-based --frame-range so
        # missing tags produce a clear error.
        safe_tag = lua_escape(tag_name)
        script = f"""
        local spr = app.activeSprite
        if not spr then print("ERROR:No active sprite") return end
        for _, tag in ipairs(spr.tags) do
            if tag.name == "{safe_tag}" then
                print("RANGE:" .. (tag.fromFrame.frameNumber - 1) .. "," .. (tag.toFrame.frameNumber - 1))
                return
            end
        end
        print("ERROR:Tag not found")
        """
        ok, out = await AsepriteCommand.execute_lua_script_checked_async(script, filename)
        if not ok:
            return f"Failed to resolve tag: {out}"
        frame_range = next(
            (line[len("RANGE:"):] for line in out.splitlines() if line.startswith("RANGE:")),
            None,
        )
        if frame_range is None:
            return "Failed to resolve tag: no range returned"
        args += ["--frame-range", frame_range]
    args.append(filename)
    if scale > 1:
        args += ["--scale", str(scale)]
    args += ["--sheet-type", sheet_type]
    if padding > 0:
        args += ["--shape-padding", str(padding)]
    if data_filename:
        err = reject_traversal(data_filename)
        if err:
            return err
        args += ["--data", data_filename, "--format", data_format]
        if list_tags:
            args.append("--list-tags")
    args += ["--sheet", output_filename]

    success, output = await AsepriteCommand.run_command_async(args)
    if success and not os.path.exists(output_filename):
        success = False
        output = "Aseprite exited 0 but wrote no sheet file"
    if success and data_filename and not os.path.exists(data_filename):
        success = False
        output = "Aseprite exited 0 but wrote no data file"
    if success:
        msg = f"Sprite sheet exported to {output_filename} ({sheet_type})"
        if data_filename:
            msg += f" with data file {data_filename}"
        return msg
    return f"Failed to export sprite sheet: {output}"


@mcp.tool()
async def export_layers(
    filename: str,
    output_directory: str,
    include_hidden: bool = False,
) -> str:
    """Export each layer as its own PNG file named <layer>.png.

    Args:
        filename: Aseprite file to export
        output_directory: Directory for the per-layer PNGs (created if missing)
        include_hidden: Also export hidden layers (default False)
    """
    if not os.path.exists(filename):
        return f"File {filename} not found"
    err = reject_traversal(output_directory)
    if err:
        return err
    os.makedirs(output_directory, exist_ok=True)

    # Layer names come from the sprite, and must never become directory paths
    # in Aseprite's {layer} output template. Sanitize a temporary clone only.
    with tempfile.TemporaryDirectory(prefix="aseprite-mcp-export-") as scratch:
        clone_path = os.path.join(scratch, "source.aseprite")
        unsafe = lua_escape(r'[/\{}<>:"|?*]')
        script = f"""
        local spr = app.activeSprite
        if not spr then print("ERROR:No active sprite") return end
        local clone = Sprite(spr)
        local names = {{}}
        local function sanitize(layers)
            for _, layer in ipairs(layers) do
                local safe = layer.name:gsub("{unsafe}", "_"):gsub("%c", "_")
                    :gsub("^[%. ]+", ""):gsub("[%. ]+$", "")
                if safe == "" or safe == "." or safe == ".." then safe = "layer" end
                local base, index = safe, 2
                while names[safe] do safe = base .. "_" .. index; index = index + 1 end
                names[safe] = true
                layer.name = safe
                if layer.isGroup then sanitize(layer.layers) end
            end
        end
        sanitize(clone.layers)
        clone:saveAs("{lua_escape(clone_path.replace(chr(92), '/'))}")
        clone:close()
        print("OK")
        """
        success, output = await AsepriteCommand.execute_lua_script_checked_async(script, filename)
        if not success:
            return f"Failed to export layers: {output}"
        args = ["--batch"]
        if include_hidden:
            args.append("--all-layers")
        args += ["--split-layers", clone_path, "--save-as", os.path.join(scratch, "{layer}.png")]
        success, output = await AsepriteCommand.run_command_async(args)
        if not success:
            return f"Failed to export layers: {output}"
        files = sorted(glob.glob(os.path.join(scratch, "*.png")))
        try:
            destinations = [validate_path(os.path.join(output_directory, os.path.basename(path))) for path in files]
        except (ValueError, OSError) as error:
            return f"Failed to export layers: {error}"
        produced = [os.path.basename(path) for path in files]
        for source, destination in zip(files, destinations):
            shutil.move(source, destination)
    if not produced:
        return "Failed to export layers: Aseprite exited 0 but wrote no PNG files"
    return f"Layers exported to {output_directory}: {', '.join(produced)}"


@mcp.tool()
async def export_tag(
    filename: str,
    tag_name: str,
    output_filename: str,
    scale: int = 1,
) -> str:
    """Export the frames of an animation tag as a GIF or PNG sequence.

    Args:
        filename: Aseprite file to export
        tag_name: Animation tag to export
        output_filename: Output path; .gif gives an animation, .png a sequence
        scale: Integer scale factor (default 1)
    """
    if not os.path.exists(filename):
        return f"File {filename} not found"
    if scale < 1 or scale > 64:
        return "scale must be between 1 and 64"
    err = reject_traversal(output_filename)
    if err:
        return err

    # --tag silently exports *all* frames (exit 0) when the tag does not
    # exist, so a produced file is not proof the tag was honoured. Validate
    # the tag up front — same approach as export_spritesheet.
    safe_tag = lua_escape(tag_name)
    check = f"""
    local spr = app.activeSprite
    if not spr then print("ERROR:No active sprite") return end
    for _, tag in ipairs(spr.tags) do
        if tag.name == "{safe_tag}" then print("OK") return end
    end
    print("ERROR:Tag not found")
    """
    ok, out = await AsepriteCommand.execute_lua_script_checked_async(check, filename)
    if not ok:
        return f"Failed to export tag: {out}"

    args = ["--batch", filename, "--tag", tag_name]
    if scale > 1:
        args += ["--scale", str(scale)]
    success, output = await _save_export(args, output_filename)
    if success:
        return f"Tag '{tag_name}' exported to {output_filename}"
    return f"Failed to export tag: {output}"


@mcp.tool()
async def import_image_as_layer(
    filename: str,
    image_path: str,
    layer_name: str,
    frame_index: int = 1,
    x: int = 0,
    y: int = 0,
) -> str:
    """Import an image file (PNG, etc.) into a layer of the sprite.

    Useful for bringing in reference images or composing pre-made parts.
    The layer is created if it does not exist. Works best when the sprite
    is in RGB color mode.

    Args:
        filename: Aseprite file to modify
        image_path: Image file to import
        layer_name: Layer to place the image on
        frame_index: Frame index starting at 1 (default 1)
        x: X position for the image's top-left corner (default 0)
        y: Y position for the image's top-left corner (default 0)
    """
    if not os.path.exists(filename):
        return f"File {filename} not found"
    if not os.path.exists(image_path):
        return f"Image {image_path} not found"

    safe_layer = lua_escape(layer_name)
    safe_image = lua_escape(os.path.abspath(image_path).replace("\\", "/"))
    script = f"""
    {FIND_LAYER}
    {NORMALIZE_CEL}
    local spr = app.activeSprite
    if not spr then print("ERROR:No active sprite") return end

    local idx = {frame_index}
    if idx < 1 or idx > #spr.frames then print("ERROR:Frame index out of range") return end

    local src = Image{{ fromFile = "{safe_image}" }}
    if not src then print("ERROR:Could not load image") return end

    local target = find_layer(spr, "{safe_layer}")
    app.transaction(function()
        if not target then
            target = spr:newLayer()
            target.name = "{safe_layer}"
        end
        local cel = normalize_cel(spr, target, spr.frames[idx], true)
        cel.image:drawImage(src, Point({x}, {y}))
    end)

    spr:saveAs(spr.filename)
    print("OK")
    """

    success, output = await AsepriteCommand.execute_lua_script_checked_async(script, filename)
    if success:
        return f"Image {image_path} imported onto '{layer_name}' frame {frame_index} in {filename}"
    return f"Failed to import image: {output}"
