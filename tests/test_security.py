"""Security regressions, including the MCP validation and HTTP boundaries."""

import asyncio
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import socket
import sys
import time
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest
from PIL import Image
from mcp.server.mcpserver.exceptions import ToolError

from conftest import run
from aseprite_mcp import mcp
from aseprite_mcp.core import commands, fonts
from aseprite_mcp.core.commands import AsepriteCommand, reject_traversal
from aseprite_mcp.core.security import MAX_ITEMS, validate_path
from aseprite_mcp.tools import canvas, drawing, export, preview, script, text


@pytest.mark.parametrize('tool', ['draw_pixels', 'draw_pixels_at'])
@pytest.mark.parametrize('coordinate', ['(function() print("INJECTED") return 0 end)()', '0', 1.5, True, None])
def test_pixel_injection_rejected_at_mcp_boundary(tmp_path, monkeypatch, tool, coordinate):
    path = tmp_path / 'sprite.aseprite'
    path.touch()
    execute = AsyncMock()
    monkeypatch.setattr(AsepriteCommand, 'execute_lua_script_checked_async', execute)
    arguments = {'filename': str(path), 'pixels': [{'x': coordinate, 'y': 0, 'color': '#fff'}]}
    if tool.endswith('_at'):
        arguments.update(layer_name='body', frame_index=1)
    with pytest.raises(ToolError):
        run(mcp.call_tool(tool, arguments))
    execute.assert_not_awaited()


@pytest.mark.parametrize('tool', [drawing.draw_pixels, drawing.draw_pixels_at])
def test_direct_pixel_call_also_rejects_injection(tmp_path, monkeypatch, tool):
    path = tmp_path / 'sprite.aseprite'
    path.touch()
    execute = AsyncMock()
    monkeypatch.setattr(AsepriteCommand, 'execute_lua_script_checked_async', execute)
    arguments = {'filename': str(path), 'pixels': [{'x': 'evil()', 'y': 0}]}
    if tool is drawing.draw_pixels_at:
        arguments.update(layer_name='body', frame_index=1)
    assert run(tool(**arguments)).startswith('Invalid input:')
    execute.assert_not_awaited()


def test_pixel_schema_describes_integer_coordinates():
    schema = next(tool.input_schema for tool in run(mcp.list_tools()) if tool.name == 'draw_pixels')
    pixel = next(value for value in schema['$defs'].values() if 'x' in value.get('properties', {}))
    assert pixel['properties']['x']['type'] == 'integer'


@pytest.mark.parametrize('size', [(4097, 1), (4096, 4096)])
def test_canvas_allocation_limited_before_execution(monkeypatch, size):
    execute = AsyncMock()
    monkeypatch.setattr(AsepriteCommand, 'execute_lua_script_checked_async', execute)
    assert run(canvas.create_canvas(*size)).startswith('Invalid input:')
    execute.assert_not_awaited()


def test_pixel_count_limited_before_script_generation(tmp_path, monkeypatch):
    path = tmp_path / 'sprite.aseprite'
    path.touch()
    execute = AsyncMock()
    monkeypatch.setattr(AsepriteCommand, 'execute_lua_script_checked_async', execute)
    assert run(drawing.draw_pixels(str(path), [{'x': 0}] * (MAX_ITEMS + 1))).startswith('Invalid input:')
    execute.assert_not_awaited()


@pytest.mark.parametrize('path', ['a/../b', '/tmp/a/../b', r'C:\a\..\b', '../b'])
def test_parent_components_rejected_before_normalization(path):
    assert reject_traversal(path) is not None


def test_workspace_scope_and_symlink_escape(tmp_path, monkeypatch):
    root = tmp_path / 'workspace'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (root / 'link').symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv('ASEPRITE_WORKSPACE_ROOT', str(root))
    assert validate_path(str(root / 'new.aseprite')) == str(root / 'new.aseprite')
    for path in [outside / 'new.aseprite', root / 'link' / 'new.aseprite']:
        with pytest.raises(ValueError):
            validate_path(str(path))
    assert run(script.run_lua_script('print("unsafe")')).startswith('Invalid input:')


def test_existing_absolute_paths_still_supported(tmp_path):
    assert validate_path(str(tmp_path / 'foo..bar.aseprite')) == str(tmp_path / 'foo..bar.aseprite')


def test_paths_checked_on_tools_without_previous_traversal_guard(tmp_path, monkeypatch):
    monkeypatch.setenv('ASEPRITE_WORKSPACE_ROOT', str(tmp_path / 'workspace'))
    assert run(canvas.add_layer(str(tmp_path / 'outside.aseprite'), 'body')).startswith('Invalid input:')
    assert run(export.export_sprite(str(tmp_path / 'outside.aseprite'), 'out')).startswith('Invalid input:')


def test_export_format_cannot_introduce_traversal():
    assert run(export.export_sprite('source.aseprite', 'out', '../secret')).startswith('Invalid input:')


@pytest.mark.parametrize('tool', [export.export_sprite, export.export_frame, export.export_spritesheet])
def test_automatic_extension_cannot_bypass_workspace(tmp_path, monkeypatch, tool):
    root = tmp_path / 'workspace'
    root.mkdir()
    (root / 'out.png').symlink_to(tmp_path / 'outside.png')
    monkeypatch.setenv('ASEPRITE_WORKSPACE_ROOT', str(root))
    args = {'filename': str(root / 'source.aseprite'), 'output_filename': str(root / 'out')}
    if tool is export.export_frame:
        args['frame_index'] = 1
    assert run(tool(**args)).startswith('Invalid input:')


def test_numbered_export_cannot_follow_symlink_outside_workspace(tmp_path, monkeypatch):
    from aseprite_mcp.tools import animation
    async def scenario():
        root = tmp_path / 'workspace'
        root.mkdir()
        path = str(root / 'source.aseprite')
        await canvas.create_canvas(8, 8, path)
        await animation.add_frames(path, 1)
        # Determine the CLI's numbering convention using a legitimate export.
        assert 'successfully' in await export.export_sprite(path, str(root / 'out.png'))
        numbered = list(root.glob('out*.png'))
        assert len(numbered) == 2
        outside = tmp_path / 'outside.png'
        outside.write_bytes(b'KEEP')
        numbered[0].unlink()
        numbered[0].symlink_to(outside)
        monkeypatch.setenv('ASEPRITE_WORKSPACE_ROOT', str(root))
        assert (await export.export_sprite(path, str(root / 'out.png'))).startswith('Failed')
        assert outside.read_bytes() == b'KEEP'
    run(scenario())


def test_large_input_sprite_rejected_before_decode(tmp_path):
    path = tmp_path / 'oversize.png'
    Image.new('RGBA', (4097, 1)).save(path)
    assert run(export.import_image_as_layer('source.aseprite', str(path), 'body')).startswith('Invalid input:')


@pytest.mark.parametrize('value', [float('nan'), float('inf'), 10**1000])
def test_nonfinite_or_extreme_numbers_rejected(value):
    assert run(canvas.create_canvas(value, 1)).startswith('Invalid input:')


def test_font_sheet_cannot_escape_font_directory(tmp_path):
    Image.new('RGBA', (1, 1)).save(tmp_path / 'outside.png')
    directory = tmp_path / 'font'
    directory.mkdir()
    (directory / 'font.json').write_text(json.dumps({'sheets': [{'file': '../outside.png', 'cell_w': 1, 'cell_h': 1, 'ascent': 1}]}))
    with pytest.raises(fonts.FontError):
        fonts.BitmapFont(str(directory))


def test_text_scale_bounded_before_font_loading():
    assert run(text.measure_text('x', 'missing', size=10**6)).startswith('Invalid input:')


def test_font_shape_bounds_raster_before_expansion():
    font = type('Font', (), {'is_bitmap': True, 'layout': lambda *args: pytest.fail('must reject before rasterization')})()
    with pytest.raises(fonts.FontError):
        fonts.shape('x', font, size=10000)


def test_truetype_letter_spacing_cannot_bypass_total_raster_limit(monkeypatch):
    monkeypatch.setattr(fonts, 'MAX_TEXT_PIXELS', 3)
    monkeypatch.setattr(fonts.ImageFont, 'truetype', lambda *args: object())
    monkeypatch.setattr(fonts.TrueTypeFont, '_raster', staticmethod(lambda *args: ({(0, 0), (1, 0)}, 2)))
    with pytest.raises(fonts.FontError, match='pixel limit'):
        fonts.TrueTypeFont('unused.ttf').layout('AA', 1, 1)


def test_font_descriptor_symlink_cannot_escape_workspace(tmp_path, monkeypatch):
    root = tmp_path / 'workspace'
    root.mkdir()
    outside = tmp_path / 'outside.json'
    outside.write_text('{}')
    (root / 'font.json').symlink_to(outside)
    monkeypatch.setenv('ASEPRITE_WORKSPACE_ROOT', str(root))
    with pytest.raises(fonts.FontError, match='outside ASEPRITE_WORKSPACE_ROOT'):
        fonts.BitmapFont(str(root))


def python_command(monkeypatch, code, timeout='0.15'):
    monkeypatch.setenv('ASEPRITE_PATH', sys.executable)
    monkeypatch.setenv('ASEPRITE_TIMEOUT_SECONDS', timeout)
    return ['-c', code]


def test_command_timeout_reaps_child(monkeypatch):
    args = python_command(monkeypatch, 'import time; time.sleep(5)')
    start = time.monotonic()
    success, output = AsepriteCommand.run_command(args)
    assert not success and 'timed out' in output
    assert time.monotonic() - start < 2


def test_command_output_is_bounded(monkeypatch):
    monkeypatch.setattr(commands, '_MAX_OUTPUT_BYTES', 1024)
    args = python_command(monkeypatch, 'print("x" * 100000)', timeout='2')
    success, output = AsepriteCommand.run_command(args)
    assert not success and 'output exceeds' in output


def test_missing_binary_reports_error(monkeypatch, tmp_path):
    monkeypatch.setenv('ASEPRITE_PATH', str(tmp_path / 'missing'))
    success, output = AsepriteCommand.run_command([])
    assert not success and output


def test_async_command_does_not_block_event_loop(monkeypatch):
    args = python_command(monkeypatch, 'import time; time.sleep(0.2)', timeout='2')
    async def scenario():
        task = asyncio.create_task(AsepriteCommand.run_command_async(args))
        start = time.monotonic()
        await asyncio.sleep(0.03)
        elapsed = time.monotonic() - start
        assert not task.done() and elapsed < 0.15
        assert (await task)[0]
    run(scenario())


def test_cancelling_command_reaps_child(monkeypatch, tmp_path):
    pid_file = tmp_path / 'child.pid'
    args = python_command(monkeypatch, f'import os,time; open({str(pid_file)!r},"w").write(str(os.getpid())); time.sleep(5)', timeout='5')
    async def scenario():
        task = asyncio.create_task(AsepriteCommand.run_command_async(args))
        for _ in range(200):
            if pid_file.exists() and pid_file.read_text():
                break
            await asyncio.sleep(0.01)
        assert pid_file.exists()
        pid = int(pid_file.read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    run(scenario())


def test_cancelled_lua_cleans_script_only_after_execution_stops(monkeypatch, tmp_path):
    files = []
    original = AsepriteCommand._script_file
    def record(*args):
        path, argv = original(*args)
        files.append(path)
        return path, argv
    async def wait_for_cancel(args):
        assert Path(files[0]).exists()
        await asyncio.sleep(5)
    monkeypatch.setattr(AsepriteCommand, '_script_file', record)
    monkeypatch.setattr(AsepriteCommand, 'run_command_async', wait_for_cancel)
    async def scenario():
        task = asyncio.create_task(AsepriteCommand.execute_lua_script_async('print("test")'))
        while not files:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not Path(files[0]).exists()
    run(scenario())


def unused_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@pytest.fixture
def http_preview(tmp_path):
    Image.new('RGBA', (2, 2)).save(tmp_path / 'sprite.png')
    (tmp_path / '.env').write_text('FAKE_SECRET')
    port = unused_port()
    message = run(preview.start_preview_server(str(tmp_path), port))
    assert message.startswith('Preview server started:'), message
    url = urlsplit(message.split(': ', 1)[1])
    try:
        yield tmp_path, port, url.path
    finally:
        run(preview.stop_preview_server(port))


def request(port, path, host=None, method='GET'):
    connection = HTTPConnection('127.0.0.1', port, timeout=3)
    headers = {'Host': host} if host else {}
    connection.request(method, path, headers=headers)
    response = connection.getresponse()
    result = response.status, response.read(), dict(response.getheaders())
    connection.close()
    return result


def test_preview_is_loopback_only_and_requires_private_url(http_preview):
    root, port, prefix = http_preview
    assert preview._servers[port][0].server_address == ('127.0.0.1', port)
    assert request(port, '/')[0] == 403
    assert request(port, prefix + 'sprite.png')[0] == 200
    assert request(port, prefix + 'sprite.png', host=f'attacker.example:{port}')[0] == 403


def test_preview_only_lists_and_serves_images(http_preview):
    root, port, prefix = http_preview
    status, body, headers = request(port, prefix)
    assert status == 200 and b'sprite.png' in body and b'.env' not in body
    assert request(port, prefix + '.env')[0] == 404
    status, body, _ = request(port, prefix + 'sprite.png', method='HEAD')
    assert status == 200 and body == b''
    assert headers['Referrer-Policy'] == 'no-referrer'


def test_preview_refuses_file_and_directory_symlinks(http_preview, tmp_path_factory):
    root, port, prefix = http_preview
    outside = tmp_path_factory.mktemp('preview-outside')
    Image.new('RGBA', (1, 1)).save(outside / 'outside.png')
    (root / 'leak.png').symlink_to(outside / 'outside.png')
    (root / 'outside').symlink_to(outside, target_is_directory=True)
    assert request(port, prefix + 'leak.png')[0] == 404
    assert request(port, prefix + 'outside/outside.png')[0] == 404
    assert request(port, prefix + '%2e%2e/outside.png')[0] == 404
    assert b'leak.png' not in request(port, prefix)[1]


def test_relative_preview_directory_works(tmp_path, monkeypatch):
    root = tmp_path / 'exports'
    root.mkdir()
    Image.new('RGBA', (1, 1)).save(root / 'image.png')
    monkeypatch.chdir(tmp_path)
    port = unused_port()
    url = run(preview.start_preview_server('exports', port)).split(': ', 1)[1]
    try:
        assert request(port, urlsplit(url).path + 'image.png')[0] == 200
    finally:
        run(preview.stop_preview_server(port))


def test_preview_reports_bind_failure(tmp_path):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        assert run(preview.start_preview_server(str(tmp_path), port)).startswith('Failed')
        assert port not in preview._servers


def test_unowned_preview_has_no_pid_signal_path(tmp_path, monkeypatch):
    monkeypatch.setattr(os, 'kill', lambda *args: pytest.fail('must never signal an unowned process'))
    assert run(preview.stop_preview_server(12345)).startswith('No preview server')


@pytest.mark.parametrize('port', [-1, 0, 65536])
def test_preview_rejects_invalid_ports(tmp_path, port):
    assert run(preview.start_preview_server(str(tmp_path), port)).startswith('Invalid')
    assert run(preview.stop_preview_server(port)).startswith('Invalid')


def test_stdio_round_trip_after_sdk_upgrade(tmp_path):
    from mcp import Client, StdioServerParameters
    async def scenario():
        parameters = StdioServerParameters(command=sys.executable, args=['-m', 'aseprite_mcp'], cwd=str(Path(__file__).resolve().parents[1]))
        async with Client(parameters, mode='legacy', read_timeout_seconds=5) as client:
            tools = await client.list_tools()
            assert any(tool.name == 'draw_pixels' for tool in tools.tools)
            result = await client.call_tool('create_canvas', {'width': 8, 'height': 8, 'filename': str(tmp_path / 'roundtrip.aseprite')})
            assert not result.is_error and (tmp_path / 'roundtrip.aseprite').exists()
            invalid = await client.call_tool('draw_pixels', {'filename': str(tmp_path / 'roundtrip.aseprite'), 'pixels': [{'x': 'print("bad")', 'y': 0}]})
            assert invalid.is_error
            # A rejected input must not crash the session.
            assert (await client.list_tools()).tools
    run(scenario())


def test_concurrent_mutations_do_not_lose_layers(tmp_path):
    from aseprite_mcp.tools import animation
    async def scenario():
        path = str(tmp_path / 'concurrent.aseprite')
        assert 'successfully' in await canvas.create_canvas(8, 8, path)
        results = await asyncio.gather(canvas.add_layer(path, 'first'), canvas.add_layer(path, 'second'))
        assert all('added' in result for result in results)
        info = json.loads(await animation.get_sprite_info(path))
        assert {'first', 'second'} <= {layer['name'] for layer in info['layers']}
    run(scenario())


def test_layer_names_cannot_escape_export_directory(tmp_path):
    from aseprite_mcp.tools import animation
    async def scenario():
        source = str(tmp_path / 'layers.aseprite')
        output = tmp_path / 'exports'
        await canvas.create_canvas(8, 8, source)
        await canvas.add_layer(source, '../escaped')
        await canvas.add_layer(source, '.._escaped')
        for name in ('../escaped', '.._escaped'):
            result = await drawing.draw_pixels_at(source, name, 1, [{'x': 0, 'y': 0, 'color': '#ff0000'}])
            assert 'Failed' not in result, result
        result = await export.export_layers(source, str(output))
        assert result.startswith('Layers exported'), result
        assert not (tmp_path / 'escaped.png').exists()
        assert len(list(output.glob('*.png'))) >= 2
        names = {layer['name'] for layer in json.loads(await animation.get_sprite_info(source))['layers']}
        assert {'../escaped', '.._escaped'} <= names
    run(scenario())
