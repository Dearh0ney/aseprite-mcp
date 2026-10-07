"""Bounded Aseprite execution, with synchronous and async entry points."""

import asyncio
import os
import signal
import subprocess
import tempfile
import threading
import time

import dotenv

from .security import MAX_SCRIPT_BYTES, validate_path

_ENV_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))
dotenv.load_dotenv(dotenv_path=_ENV_PATH)
_SLOTS = threading.BoundedSemaphore(2)
_MAX_OUTPUT_BYTES = 2_097_152


def lua_escape(s: str) -> str:
    return (s.replace('\\', '\\\\').replace('"', '\\"')
            .replace('\n', '\\n').replace('\r', '\\r').replace('\0', '\\000'))


def reject_traversal(path: str) -> str | None:
    try:
        validate_path(path)
    except (ValueError, OSError) as error:
        return f'Invalid filename: {error}'
    return None


def _terminate(process):
    """Terminate only the process tree created for this command."""
    if os.name == 'posix':
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        if process.poll() is None:
            try:
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass  # Still kill our direct child if taskkill is unavailable.
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)


async def _in_worker(function, *args):
    cancelled = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(function, *args, cancel_event=cancelled))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancelled.set()
        # Keep temporary scripts until the child has actually been reaped.
        await asyncio.shield(task)
        raise


class AsepriteCommand:
    @staticmethod
    def run_command(args, *, cancel_event=None):
        cancelled = cancel_event or threading.Event()
        try:
            timeout = float(os.getenv('ASEPRITE_TIMEOUT_SECONDS', '30'))
            if not 0 < timeout <= 300:
                return False, 'ASEPRITE_TIMEOUT_SECONDS must be > 0 and <= 300'
        except ValueError:
            return False, 'Invalid ASEPRITE_TIMEOUT_SECONDS'
        deadline = time.monotonic() + timeout
        acquired = False
        process = None
        readers = []
        outputs = [bytearray(), bytearray()]
        lock = threading.Lock()
        exceeded = threading.Event()
        try:
            while not acquired:
                if cancelled.is_set():
                    return False, 'Command cancelled'
                if time.monotonic() >= deadline:
                    return False, f'Command timed out after {timeout:g}s'
                acquired = _SLOTS.acquire(timeout=min(0.02, timeout))
            command = [os.getenv('ASEPRITE_PATH', 'aseprite')] + args
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=(os.name == 'posix'))

            def read_output(pipe, buffer):
                try:
                    while block := pipe.read(65536):
                        with lock:
                            remaining = _MAX_OUTPUT_BYTES - sum(map(len, outputs))
                            buffer.extend(block[:max(0, remaining)])
                            if len(block) > remaining:
                                exceeded.set()
                                break
                finally:
                    pipe.close()

            for pipe, buffer in zip((process.stdout, process.stderr), outputs):
                reader = threading.Thread(target=read_output, args=(pipe, buffer), daemon=True)
                reader.start()
                readers.append(reader)
            error = None
            while process.poll() is None or any(reader.is_alive() for reader in readers):
                if cancelled.is_set():
                    error = 'Command cancelled'
                elif exceeded.is_set():
                    error = f'Command output exceeds {_MAX_OUTPUT_BYTES} bytes'
                elif time.monotonic() >= deadline:
                    error = f'Command timed out after {timeout:g}s'
                if error:
                    _terminate(process)
                    break
                time.sleep(0.01)
            for reader in readers:
                reader.join(timeout=1)
            if exceeded.is_set():
                error = f'Command output exceeds {_MAX_OUTPUT_BYTES} bytes'
            if error:
                return False, error
            stdout, stderr = (bytes(buffer).decode('utf-8', errors='replace') for buffer in outputs)
            return (True, stdout) if process.returncode == 0 else (False, stderr or stdout)
        except (OSError, subprocess.SubprocessError) as error:
            return False, str(error)
        finally:
            try:
                if process is not None and process.poll() is None:
                    _terminate(process)
            finally:
                if acquired:
                    _SLOTS.release()

    @staticmethod
    async def run_command_async(args):
        return await _in_worker(AsepriteCommand.run_command, args)

    @staticmethod
    def _script_file(script_content, filename):
        if len(script_content.encode('utf-8')) > MAX_SCRIPT_BYTES:
            raise ValueError(f'Script exceeds {MAX_SCRIPT_BYTES} bytes')
        if filename:
            filename = validate_path(filename)
            if not os.path.isfile(filename):
                raise ValueError(f'File {filename} not found')
        with tempfile.NamedTemporaryFile(suffix='.lua', delete=False, mode='w', encoding='utf-8') as file:
            file.write(script_content)
            path = file.name
        args = ['--batch']
        if filename:
            args.append(filename)
        args.extend(['--script', path])
        return path, args

    @staticmethod
    def execute_lua_script(script_content, filename=None):
        try:
            path, args = AsepriteCommand._script_file(script_content, filename)
        except (ValueError, OSError) as error:
            return False, str(error)
        try:
            return AsepriteCommand.run_command(args)
        finally:
            os.unlink(path)

    @staticmethod
    async def execute_lua_script_async(script_content, filename=None):
        try:
            path, args = AsepriteCommand._script_file(script_content, filename)
        except (ValueError, OSError) as error:
            return False, str(error)
        try:
            return await AsepriteCommand.run_command_async(args)
        finally:
            os.unlink(path)

    @staticmethod
    def _checked(success, output):
        if not success:
            return False, output
        for line in output.splitlines():
            if line.startswith('ERROR:'):
                return False, line[len('ERROR:'):]
        return True, output

    @staticmethod
    def execute_lua_script_checked(script_content, filename=None):
        return AsepriteCommand._checked(*AsepriteCommand.execute_lua_script(script_content, filename))

    @staticmethod
    async def execute_lua_script_checked_async(script_content, filename=None):
        return AsepriteCommand._checked(*await AsepriteCommand.execute_lua_script_async(script_content, filename))
